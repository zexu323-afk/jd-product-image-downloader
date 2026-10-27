# 独立分享版验证

日期：2026-10-27。

| 检查 | 结果 |
|---|---|
| Python 自动测试 | 14 项通过，0 失败 |
| 离线演示 | 通过；1 张原创主图 + 1 张原创详情图，ZIP 和 manifest 完成 |
| 文件与 ZIP 完整性 | 自动核对 SHA-256、字节数及归档内容 |
| 错误边界 | 覆盖平台 403、HTML 验证响应、网络失败、非允许域名、重定向及路径越界 |
| 失败输出 | 不发布成功 manifest；同会话拒绝覆盖 |
| 发现逻辑 | 覆盖主图顺序、800×800 规范化、详情来源筛选及去重 |
| 代码语法 | `compileall` 通过 |
| 浏览器运行时预检 | 缺失/任意目录拒绝运行，不导入浏览器管理器、不下载、不清理输入目录 |
| 真实京东采集 | 本次未执行，不用离线通过替代真实验收 |

可在仓库根目录重复执行：

```powershell
$env:PYTHONPATH = "$PWD/src"
python -m unittest discover -s tests -v
python -m jd_images demo
python -m compileall -q src tests
```

测试和演示产生的临时文件均位于仓库 `.local/`，不纳入 Git。测试使用 Python 标准库，不需要安装浏览器依赖或连接真实平台。
