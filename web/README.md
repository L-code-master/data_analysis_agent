# 前端开发环境说明

## 快速启动

需要**两个终端**（后端 + 前端）。

### 终端 1：后端

```powershell
conda activate data_agent
cd D:\WorkSpace\data_analysis_agent
uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1 --reload
```

### 终端 2：前端

```powershell
cd D:\WorkSpace\data_analysis_agent\web
npm run dev
```

打开 **http://127.0.0.1:5173**

---

## 访问地址

| 服务 | 地址 |
|---|---|
| 前端 | http://127.0.0.1:5173 |
| 后端 API | http://127.0.0.1:8000/api/v1 |
| API 文档（Swagger） | http://127.0.0.1:8000/docs |
| 健康检查 | http://127.0.0.1:8000/api/v1/health |

> 前端通过 Vite 代理把 `/api` 转发到后端:8000，因此**不需要配置 CORS**。

---

## 首次使用流程

> v1.1 起注册即**赠送 1 次体验诊断**（`SIGNUP_FREE_CREDITS`，默认 1），
> 且无需真实数据 —— 上传页可下载**样例 Excel**（含 6 张报表 + 说明页，
> 上传时自动拆分），5 分钟走完整流程。

1. **注册**：http://127.0.0.1:5173/register
   - 店铺名称、邮箱、密码（≥10 位，含两类字符）
   - 注册成功后顶栏显示「剩余 1 次」
2. **（可选）快速充值**：体验次数用完后在「购买次数」选套餐
   - 人工核销模式下，需要管理员确认收款
   - **开发环境快速充值**：先在页面下单，记下订单号，然后：
     ```powershell
     curl -X POST "http://127.0.0.1:8000/api/v1/billing/orders/<订单号>/confirm" `
          -H "Authorization: Bearer <access_token>"
     ```
3. **上传数据**：左侧「上传数据」→ 拖入千牛导出文件
   - 没有数据？点页面上的「下载样例数据」，把下载的 Excel 直接拖进来
   - 一个 Excel 含多张报表会**自动拆分**为多个数据源（说明页自动跳过）
4. **确认映射**：上传后点「确认字段映射」→ 核对 → 确认
   （样例数据列名精确匹配，通常零低置信度；同结构文件第二次免确认）
5. **开始诊断**：选时间范围 → 开始诊断（样例数据用「最近30天」）
6. **查看报告**：诊断完成后自动跳转
   - 报告含统计卡片、双轴趋势图、指标表与口径声明，可打印

---

## 关于浏览器控制台里的 401

如果**未登录**时访问首页，前端会跳转到 `/login`。
若控制台出现一次 401，是 token 校验的正常行为，不影响使用。

---

## 构建生产版本

```powershell
cd web
npm run build      # 产出 web/dist/
```

生产环境由 Nginx 托管 `web/dist/`，并把 `/api` 反代到 uvicorn。
**注意 SSE 必须关闭 Nginx 缓冲** —— 见 `docs/04-云服务器部署.md`。

---

## 环境依赖说明（重要）

本项目的 npm 安装/构建在 DSH 沙箱内需要放宽权限，原因是
esbuild 会启动子进程并写临时目录。**在普通终端里执行没有此限制。**

若遇到 `spawn EPERM` 或 `Access is denied`，设置临时目录到工作区内：

```powershell
$env:TMP = "D:\WorkSpace\data_analysis_agent\.esbtmp"
$env:TEMP = $env:TMP
npm run build
```

`.npmcache/` 与 `.esbtmp/` 已在 `.gitignore` 中忽略。