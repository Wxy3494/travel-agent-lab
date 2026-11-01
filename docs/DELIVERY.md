# 本地交付与复现

当前日期、回归结果和验证范围见[统一验收摘要](ACCEPTANCE.md)。这是源码交付包，不是包含Python和虚拟环境的独立可执行程序。

## 交付文件

- `artifacts/release/travel-agent-lab-source.zip`：源码、测试、配置样例、文档、CSS、背景JPG。
- `artifacts/release/manifest.json`：ZIP的SHA256、每个文件哈希、最终Git提交和工作区状态。
- `artifacts/release/history.bundle`：仅公开源码的本地Git历史；ZIP刻意不带`.git`。
- `artifacts/release/release-check.json`、`wheel-check.json`：本轮发布包及wheel资源校验结果；当前原始测试证据及浏览器截图位于`artifacts/chat-response-20261101/`，此前证据保留在`artifacts/assistant-20261101/`、`artifacts/navigation-20261101/`、`artifacts/product-20261101/`、`artifacts/field-errors-20261101/`、`artifacts/home-transport-20261031/`、`artifacts/meituan-20261031/`、`artifacts/admissions-20261031/`和`artifacts/fixes-20261031/`。

密钥、SQLite、运行日志、个人行程、虚拟环境和原始评测结果不纳入源码ZIP或Git历史。没有上传、推送或发布。尚未选定开源许可证；本地交付不意味着授予任意开源使用权。

## 在其他机器上准备环境

1. 解压源码ZIP，安装兼容的Python（本次验证为Windows Python 3.12）。
2. 在解压目录创建环境并安装锁定依赖：

   ```powershell
   python -m venv .venv
   .\.venv\Scripts\python.exe -m pip install -r requirements.lock
   .\.venv\Scripts\python.exe -m pip install --no-deps -e .
   ```

3. 演示模式不需要密钥；真实接口配置按`.env.example`在本机填写`.env`。
4. 双击`启动行旅.cmd`，等待服务就绪并自动打开页面。

本轮验证ZIP在独立目录与独立进程中执行界面初始化，确认CSS读入和背景图嵌入；wheel也检查所带资源。源码界面另用本机 Chrome 和独立演示服务验证错误提示位置、修正清除、自动展开与正常提交，并保存截图。测试复用本机依赖，没有假定已完成全新机器安装、Linux或Docker验收。

## 校验与版本恢复

在维护目录运行：

```powershell
.\.venv\Scripts\python.exe -m scripts.package_public
.\.venv\Scripts\python.exe -m scripts.check_release --smoke-ui
git bundle verify artifacts/release/history.bundle
```

接收方需要完整历史时，在空的新目标目录使用`git clone history.bundle 项目目录`，而不是把另一份源码覆盖在当前目录上。要验证包的身份，应比较发送方提供的ZIP SHA256；清单的完整性校验不等于第三方数字签名。

修改源码后先重新测试，再运行`python -m scripts.record_acceptance --evidence-dir artifacts/chat-response-20261101`更新摘要，提交修复后重新打包。脚本将检查回归、规范和依赖报告，并记录源码哈希；不要手改测试数量来冒充通过。历史快照保持原日期，新包使用新的清单与Git提交。

美团 CLI 位于本机 `.local`，不随源码包发送；新机器需按 [美团接入说明](MEITUAN.md)安装固定版本组件并填写自己的本地凭证。当前机器已配置完成。
