# Security

## Supported scope

0.2.x 仅支持研究、模拟盘和不会提交的影子意图。不要向本项目提供具有交易或提现权限的 API 密钥。

## Deployment

- 设置至少32字符随机 `CQ_API_TOKEN`。
- 使用 HTTPS 反向代理；不要直接公开 Uvicorn 端口。
- 将 SQLite 数据目录放在加密磁盘并定期备份。
- 保持 Docker 的非 root、只读文件系统、`cap_drop: ALL` 和 `no-new-privileges`。
- 查看 GitHub Dependabot、pip-audit 和 CodeQL 结果后再升级依赖。

## Reporting

发现漏洞时，请通过仓库的私有安全报告渠道提交，不要在公开 Issue 中附带 Token、账户数据或日志中的敏感信息。
