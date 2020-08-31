#!/usr/bin/env python3

"""build_stats.py: Gather statistics about sizes of dependencies.

This tools computes a realistic estimate of the size contribution to a binary
from a statically linked library. Statically linked libraries compiled with
-ffunction-sections and linked -gc-sections mean that we could drop part of the
library at the final binary linking time. This tool takes that into account the
symbols that end up in the final binary and not just all the symbols of the
components.
"""

import argparse
import collections
import json
import os
import subprocess
import sys

Symbol = collections.namedtuple('Symbol', ['address', 'size', 'typ', 'name'])

ObjectStats = collections.namedtuple('ObjectStats',
                                     ['name', 'in_partition', 'size_map'])

# Sections that end up in the binary file.
# t - text (code), d - global non-const data, n/r - read-only data,
# w - weak symbols (likely inline code not inlined),
# v - weak symbols (vtable / typeinfo)
BIN_SIZE = 'tdnrwv'

# Sections that end up in static RAM.
RAM_SIZE = 'dbs'

# u - symbols imported from some other library
# a - absolute address symbols
IGNORE_SYMBOLS = 'ua'

SIMD_NAMESPACES = [
    'N_SCALAR', 'N_WASM', 'N_NEON', 'N_PPC8', 'N_SSE4', 'N_AVX2', 'N_AVX3']


def LoadSymbols(filename):
  ret = []
  nmout = subprocess.check_output(['nm', '--format=posix', filename])
  for line in nmout.decode('utf-8').splitlines():
    if line.rstrip().endswith(':'):
      # Ignore object names.
      continue
    # symbol_name, symbol_type, (optional) address, (optional) size
    symlist = line.rstrip().split(' ')
    assert 2 <= len(symlist) <= 4
    ret.append(Symbol(
        int(symlist[2], 16) if len(symlist) > 2 else None,
        int(symlist[3], 16) if len(symlist) > 3 else None,
        symlist[1],
        symlist[0]))
  return ret


def TargetSize(symbols, symbol_filter=None):
  ret = {}
  for sym in symbols:
    if not sym.size or (symbol_filter is not None and
                        sym.name not in symbol_filter):
      continue
    t = sym.typ.lower()
    # We can remove symbols if they appear in multiple objects since they will
    # be merged by the linker.
    if symbol_filter is not None and (t == sym.typ or t in 'wv'):
      symbol_filter.remove(sym.name)
    ret.setdefault(t, 0)
    ret[t] += sym.size
  return ret


def PrintStats(stats):
  """Print a table with the size stats for a target"""
  table = []
  sum_bin_size = 0
  sum_ram_size = 0

  for objstat in stats:
    bin_size = 0
    ram_size = 0
    for typ, size in objstat.size_map.items():
      if typ in BIN_SIZE:
        bin_size += size
      if typ in RAM_SIZE:
        ram_size += size
      if typ not in BIN_SIZE + RAM_SIZE:
        raise Exception('Unknown type "%s"' % typ)
    if objstat.in_partition:
      sum_bin_size += bin_size
      sum_ram_size += ram_size

    table.append((objstat.name, bin_size, ram_size))
  mx_bin_size = max(row[1] for row in table)
  mx_ram_size = max(row[2] for row in table)

  table.append(('-- unknown --', mx_bin_size - sum_bin_size,
                mx_ram_size - sum_ram_size))

  # Print the table
  print('%-32s %17s %17s' % ('Object name', 'Binary size', 'Static RAM size'))
  for name, bin_size, ram_size in table:
    print('%-32s %8d (%5.1f%%) %8d (%5.1f%%)' % (
        name, bin_size, 100. * bin_size / mx_bin_size,
        ram_size, (100. * ram_size / mx_ram_size) if mx_ram_size else 0))
  print()


def SizeStats(args):
  """Main entry point of the program after parsing parameters.

  Computes the size statistics of the given targets and their components."""
  stats = []

  syms = {}
  for target in args.target:
    tgt_stats = []
    stdout = subprocess.check_output(
        ['ninja', '-C', args.build_dir, '-t', 'commands', target])
    # The last command is always the command to build (link) the requested
    # target.
    link_command = stdout.splitlines()[-1]

    tgt_libs = []
    link_params = link_command.decode('utf-8').split()
    for entry in link_params:
      if not entry or not (entry.endswith('.o') or entry.endswith('.a')):
        continue
      fn = os.path.join(args.build_dir, entry)
      if not os.path.exists(fn):
        continue
      if entry in tgt_libs:
        continue
      tgt_libs.append(entry)
      if entry not in syms:
        syms[entry] = LoadSymbols(fn)

    target_filename = link_params[link_params.index('-o') + 1]
    tgt_syms = LoadSymbols(os.path.join(args.build_dir, target_filename))
    used_syms = set()
    for sym in tgt_syms:
      if sym.typ.lower() in BIN_SIZE + RAM_SIZE:
        used_syms.add(sym.name)
      elif sym.typ.lower() in IGNORE_SYMBOLS:
        continue
      else:
        print('Unknown: %s %s' % (sym.typ, sym.name))

    tgt_size = TargetSize(tgt_syms)
    tgt_stats.append(ObjectStats(target, False, tgt_size))

    # Split out by SIMD.
    for namespace in SIMD_NAMESPACES:
      mangled = str(len(namespace)) + namespace
      if not any(mangled in sym.name for sym in tgt_syms):
        continue
      ret = {}
      for sym in tgt_syms:
        if not sym.size or not mangled in sym.name:
          continue
        t = sym.typ.lower()
        ret.setdefault(t, 0)
        ret[t] += sym.size
      # SIMD namespaces are not part of the partition, they are already included
      # in the jpegxl-static normally.
      if not ret:
        continue
      tgt_stats.append(ObjectStats('\\--> ' + namespace, False, ret))

    for obj in tgt_libs:
      obj_size = TargetSize(syms[obj], used_syms)
      if not obj_size:
        continue
      tgt_stats.append(ObjectStats(os.path.basename(obj), True, obj_size))
    PrintStats(tgt_stats)
    stats.append(tgt_stats)

  if args.save:
    with open(args.save, 'w') as f:
      json.dump(stats, f)


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument('target', type=str, nargs='+',
                      help='target(s) to analyze')
  parser.add_argument('--build-dir', default='build',
                      help='path to the build directory')
  parser.add_argument('--save', default=None,
                      help='path to save the stats as JSON file')
  args = parser.parse_args()
  SizeStats(args)


if __name__ == '__main__':
  main()
