#!/bin/bash
# 在 DeepSeek 官方接口（https://api.deepseek.com，模型 deepseek-v4-pro，关闭思考模式）上跑 AI 解读 30 题评测。
# 1.0 改用 DeepSeek 时用这个（类目页登记的是 DeepSeek，创始人 11/2）。参数和 run-eval-hy3.sh 一样，地址不用写；
# 运行后提示输入 DeepSeek API Key：输入时不显示，不写进文件和命令历史，用完清掉。
exec "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)/run-eval-hy3.sh" --deepseek "$@"
