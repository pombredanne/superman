import numpy as np
from collections import defaultdict

from construct import (
    Array, Enum, LFloat32, LFloat64, Magic, OnDemand, Pointer, Container,
    RepeatUntil, String, Struct, Switch, ULInt16, ULInt32, If
)
from construct_utils import BitSplitter, FixedSizeCString, FunctionSwitch


BlockType = BitSplitter(ULInt32('BlockType'),
                        complex=(0, 2), type=(2, 2), param=(4, 6),
                        data=(10, 7), deriv=(17, 2), extend=(19, 3))

BlockType_decoder = {
    'complex': {0: '', 1: 'real', 2: 'imaginary', 3: 'amplitude'},
    'type': {0: '', 1: 'sample', 2: 'reference', 3: 'ratio'},
    'deriv': {0: '', 1: 'first deriv', 2: 'second deriv', 3: 'nth deriv'},
    'extend': {0: '', 1: 'compound info', 2: 'peak table',
               3: 'molecular structure', 4: 'macro', 5: 'command log'},
    'data': {0: '', 1: 'spectrum, undefined Y units', 2: 'interferogram',
             3: 'phase spectrum', 4: 'absorbance spectrum',
             5: 'transmittance spectrum', 6: 'kubelka-munck spectrum',
             7: 'trace', 8: 'gc file (interferograms)', 9: 'gc file (spectra)',
             10: 'raman spectrum', 11: 'emission spectrum',
             12: 'reflectance spectrum', 13: 'directory',
             14: 'power spectrum', 15: 'neg. log reflectance',
             16: 'ATR spectrum', 17: 'photoacoustic spectrum',
             18: 'arithmetics (transmittance)', 19: 'arithmetics (absorbance)'},
    'param': {0: '', 1: 'data status', 2: 'instrument status', 3: 'acquisition',
              4: 'FT', 5: 'plot/display', 6: 'processing', 7: 'GC',
              8: 'library search', 9: 'communication', 10: 'sample origin'}
}
# Wrap in defaultdicts.
for k in BlockType_decoder.keys():
  BlockType_decoder[k] = defaultdict(lambda: 'unknown', BlockType_decoder[k])


def prettyprint_blocktype(bt):
  res = []
  for key in ('complex','type','deriv','extend','data','param'):
    name = BlockType_decoder[key][bt[key]]
    if name:
      res.append(name)
  if bt.param != 0:
    res.append('parameters')
  return ' '.join(res)

Parameter = Struct(
    'Parameter',
    FixedSizeCString('Name', lambda ctx: 4),  # 4 bytes, 3 chars + null
    Enum(ULInt16('Type'), INT32=0, REAL64=1, STRING=2, ENUM=3, SENUM=4),
    ULInt16('ReservedSpace'),
    If(  # Only look for a Value if this isn't the END pseudo-parameter.
        lambda ctx: ctx.Name != 'END',
        Switch('Value', lambda ctx: ctx.Type, {
            'INT32': ULInt32(''),
            'REAL64': LFloat64(''),
            'STRING': FixedSizeCString('', lambda ctx: ctx.ReservedSpace*2),
            'ENUM': FixedSizeCString('', lambda ctx: ctx.ReservedSpace*2),
            'SENUM': FixedSizeCString('', lambda ctx: ctx.ReservedSpace*2),
        })
    )
)


def is_ParameterList(block):
  bt = block.BlockType
  return bt.param != 0 or bt.extend == 1

ParameterList = RepeatUntil(lambda obj, ctx: obj.Name == 'END', Parameter)
FloatData = Array(lambda ctx: ctx.BlockLength, LFloat32(''))
StringData = String(None, lambda ctx: ctx.BlockLength*4)

DirectoryEntry = Struct(
    'Directory',
    BlockType,
    ULInt32('BlockLength'),
    ULInt32('DataPtr'),
    Pointer(
        lambda ctx: ctx.DataPtr,
        FunctionSwitch('Block', [
            (is_ParameterList, ParameterList),
            (lambda ctx: ctx.BlockType.data not in (0,13), OnDemand(FloatData)),
            (lambda ctx: ctx.BlockType.extend != 0, OnDemand(StringData))
        ])
    )
)

# The entire file.
OpusFile = Struct(
    'OpusFile',
    Magic('\n\n\xfe\xfe'),  # 0x0a0afefe
    LFloat64('Version'),
    ULInt32('FirstDirPtr'),
    ULInt32('MaxDirSize'),
    ULInt32('CurrDirSize'),
    Pointer(lambda ctx: ctx.FirstDirPtr,
            Array(lambda ctx: ctx.MaxDirSize, DirectoryEntry)))


def iter_blocks(opus_data):
  for d in opus_data.Directory:
    if d.DataPtr == 0:
      break
    label = prettyprint_blocktype(d.BlockType)
    yield label, d


def prettyprint_opus(data):
  np.set_printoptions(precision=4, suppress=True)
  print 'OPUS file, version', data.Version
  print 'Parsed', data.CurrDirSize, 'directory blocks'
  for i, (label, d) in enumerate(iter_blocks(data)):
    print i+1, label,
    if d.Block is None:
      print '(bytes %d-%d)' % (d.DataPtr, d.DataPtr+d.BlockLength*4)
      continue
    print ':'
    if is_ParameterList(d):
      for p in d.Block[:-1]:  # Don't bother printing the END block.
        print '   ', p.Name, p.Value
    else:
      foo = np.array(d.Block.value)
      print '    data:', foo.shape, foo


def plot_opus(data, title_pattern=''):
  from matplotlib import pyplot
  plot_info = defaultdict(dict)
  for label, d in iter_blocks(data):
    if d.Block is None or d.BlockType.data == 0:
      continue
    if label.endswith('data status parameters'):
      key = label[:-23]
      plot_info[key]['params'] = dict((p.Name, p.Value) for p in d.Block)
    else:
      plot_info[label]['data'] = np.array(d.Block.value)
  DXU_values = {
      'WN': 'Wavenumber (1/cm)', 'MI': 'Micron', 'LGW': 'log Wavenumber',
      'MIN': 'Minutes', 'PNT': 'Points'
  }
  for label, foo in plot_info.iteritems():
    y_type, title = label.split(' ', 1)
    if title_pattern not in title:
      print 'Skipping "%s"' % title
      continue
    params = foo['params']
    x_units = DXU_values[params['DXU']]
    y_vals = foo['data'] * params['CSF']  # CSF == scale factor
    x_vals = np.linspace(params['FXV'], params['LXV'], len(y_vals))

    pyplot.figure()
    pyplot.plot(x_vals, y_vals)
    pyplot.title(title)
    pyplot.xlabel(x_units)
    pyplot.ylabel(y_type)
  pyplot.show()


def parse_traj(fh, return_params=False):
  '''Parses out the "ratio" data from an OPUS file.'''
  # Parser requires binary file mode
  if hasattr(fh, 'mode') and 'b' not in fh.mode:
    fh = open(fh.name, 'rb')
  data = OpusFile.parse_stream(fh)
  for label, d in iter_blocks(data):
    if label == 'sample origin parameters':
      sample_params = dict((p.Name, p.Value) for p in d.Block)
      continue
    if 'ratio' not in label:
      continue
    if label.endswith('data status parameters'):
      params = dict((p.Name, p.Value) for p in d.Block)
    else:
      y_vals = np.array(d.Block.value)
      # Hacky fix for a strange issue where the first/last value is exactly zero
      if y_vals[0] == 0 and y_vals[1] > 1.0:
        y_vals = y_vals[1:]
      if y_vals[-1] == 0 and y_vals[-2] > 1.0:
        y_vals = y_vals[:-1]

  y_vals *= params['CSF']  # CSF == scale factor
  x_vals = np.linspace(params['FXV'], params['LXV'], len(y_vals))
  traj = np.transpose((x_vals, y_vals))
  # Some spectra are flipped.
  if traj[0,0] > traj[-1,0]:
    traj = traj[::-1]
  if return_params:
    return traj, sample_params
  return traj


def write_opus(fname, traj, comments):
  '''Write an OPUS file to `fname`.'''
  # Ensure comment length is a multiple of 4.
  nc = len(comments)
  if nc % 4 != 0:
    nc = ((nc//4)+1)*4
    comments = comments.ljust(nc, ' ')

  # Sanity check band step size.
  bands, ampl = traj.T
  db = np.diff(bands)
  if np.std(db) > 0.001:
    # Resample to the mean band step size.
    new_bands = np.linspace(bands[0], bands[-1], bands.size)
    ampl = np.interp(new_bands, bands, ampl)
    bands = new_bands

  meta_param = [
      Container(Name='DPF', Type='INT32', Value=1, ReservedSpace=0),
      Container(Name='NPT', Type='INT32', Value=bands.size, ReservedSpace=0),
      Container(Name='FXV', Type='INT32', Value=bands[0], ReservedSpace=0),
      Container(Name='LXV', Type='INT32', Value=bands[-1], ReservedSpace=0),
      Container(Name='CSF', Type='REAL64', Value=1.0, ReservedSpace=0),
      Container(Name='MXY', Type='REAL64', Value=ampl.max(), ReservedSpace=0),
      Container(Name='MNY', Type='REAL64', Value=ampl.min(), ReservedSpace=0),
      Container(Name='DXU', Type='ENUM', Value='WN', ReservedSpace=2),
      Container(Name='END', Type='INT32', Value=0, ReservedSpace=0)
  ]
  meta_param_size = 30  # 3 for each param, +1 for each 64-bit

  # Block types for each directory block.
  dir_bt = Container(deriv=0, extend=0, data=13, param=0, complex=0, type=0)
  data_bt = Container(deriv=0, extend=0, data=1, param=0, complex=1, type=3)
  meta_bt = Container(deriv=0, extend=0, data=1, param=1, complex=1, type=3)
  comment_bt = Container(deriv=0, extend=5, data=0, param=0, complex=0, type=0)

  # Directory blocks, with zeros where data will be filled in later.
  directory = [
      Container(BlockType=dir_bt, DataPtr=0, BlockLength=0, Block=None),
      Container(BlockType=data_bt, DataPtr=0,
                BlockLength=ampl.size, Block=ampl),
      Container(BlockType=meta_bt, DataPtr=0,
                BlockLength=meta_param_size, Block=meta_param),
      Container(BlockType=comment_bt, DataPtr=0,
                BlockLength=nc//4, Block=comments)
  ]

  # Fill in directory block information.
  ptr = 24
  directory[0].BlockLength = len(directory) * 3
  for d in directory:
    d.DataPtr = ptr
    ptr += d.BlockLength * 4

  # Assemble the whole file and write it to disk.
  opus_obj = Container(Version=920622.0, FirstDirPtr=24,
                       MaxDirSize=len(directory), CurrDirSize=len(directory),
                       Directory=directory)
  with open(fname, 'wb') as fh:
    OpusFile.build_stream(opus_obj, fh)


if __name__ == '__main__':
  from optparse import OptionParser
  op = OptionParser()
  op.add_option('--print', action='store_true', dest='_print')
  op.add_option('--plot', action='store_true')
  op.add_option('--filter', type=str, default='',
                help='Only show plots with titles matching this substring.')
  opts, args = op.parse_args()
  if len(args) != 1:
    op.error('Supply exactly one filename argument.')
  if not (opts._print or opts.plot):
    op.error('Must supply either --plot or --print.')
  data = OpusFile.parse_stream(open(args[0], 'rb'))
  if opts._print:
    prettyprint_opus(data)
  if opts.plot:
    plot_opus(data, opts.filter)
