"""Keep nested Node/Python calls on the same verified runtime as their parent."""
import os
import shutil
import sys

os.environ['NORU_PYTHON'] = sys.executable
os.environ['PYTHONUTF8'] = '1'
NODE = os.environ.get('NORU_NODE') or shutil.which('node') or 'node'
