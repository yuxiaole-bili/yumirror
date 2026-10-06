# 拉取请求

## 改了什么

<!-- 一两句话说明动机与影响面 -->

## 怎么验证的

<!-- 贴测试输出或复现步骤；涉及界面的建议附截图 -->

```
python tests/smoke_test.py
python tests/security_test.py
```

## 检查清单

- [ ] `tests/smoke_test.py` 全绿（50 项）
- [ ] `tests/security_test.py` 全绿（37~38 项）
- [ ] 动了积木/模板 → 已跑 `python scripts/gen_docs.py` 并提交生成结果
- [ ] 没有提交 `config.json` / `users.json` / `certs/` / 私钥 / 日志
- [ ] 用户可见的变化已写入 `CHANGELOG.md`
- [ ] 新功能带了测试
- [ ] 只在 Python 3.10 也支持的语法范围内（见 CONTRIBUTING.md 的兼容性红线）
- [ ] 涉及认证/加密/路径/流程执行的改动，已在描述里说明威胁模型
