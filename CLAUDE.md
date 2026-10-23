# 经营诊断 Agent（淘宝店铺数据分析）

## 项目概述

给淘宝卖家做经营诊断的 SaaS：上传千牛导出报表（订单/生意参谋/账房/退款/广告/采购），
自动识别字段、语义层按官方口径算指标（16 个）、产出每个数字可溯源的 HTML 报告。
按次收费（¥49 体验 / ¥199 单次 / ¥799 五次包），注册赠送 1 次体验。

- 后端 `app/`（FastAPI + DuckDB + SQLite，单 worker）
- 前端 `web/`（Vite + React + TS）
- 语义层 `semantic/schema.sql`（STG → ETL → 视图 → 指标 → 质量门）

## 关键命令

```powershell
# 环境：conda data_agent（Python 3.12）
.\start.ps1                        # 一键启动前后端（5173 / 8000）
python tests/run_all.py            # 模块级回归（19/20 OK；全栈冒烟需服务运行）
python tests/smoke_fullstack.py    # 全栈冒烟（需先 start.ps1，走 Vite 代理）
python tests/test_module8_sample.py# 样例数据闭环（48 断言）
```

Python 解释器路径：`D:\Aisoft\anaconda3\envs\data_agent\python.exe`

## 执行策略

1. 改完必须验证：跑对应模块测试，最后 `run_all.py` 全量回归
2. 文档与代码同步：设计变更回写 `docs/01`（§8 偏差记录），架构图在 `docs/02`
3. bash 输出乱码时改用 read_file 直接检查代码
4. 上线配置清单在 `docs/08`（支付宝、密钥、CORS 等不要凭记忆配）

## Compact Instructions

压缩时保留：当前修改的文件列表、测试结果、下一步计划
