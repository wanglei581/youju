"""给构建目录写入不可省略的提交标识；内容只有完整 Git SHA。"""

import argparse
import json
import re
import subprocess
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--revision', default='HEAD')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    sha = subprocess.check_output(['git', '-C', str(root), 'rev-parse', '--verify', '--end-of-options', args.revision + '^{commit}'], text=True).strip()
    if not re.fullmatch(r'[0-9a-f]{40}', sha):
        raise SystemExit('不是完整的 Git 提交')
    if args.output is None:
        head = subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True).strip()
        dirty = subprocess.check_output(['git', '-C', str(root), 'status', '--porcelain'], text=True).strip()
        if dirty or sha != head:
            raise SystemExit('直接构建必须使用干净工作区的 HEAD；历史提交请走 git archive 部署脚本')
    target = args.output or root / 'backend/src/youju/_build.json'
    target.write_text(json.dumps({'revision': sha}) + '\n', encoding='utf-8')
    print('构建版本：' + sha)


if __name__ == '__main__':
    main()
