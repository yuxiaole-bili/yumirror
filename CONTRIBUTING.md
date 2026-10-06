# 贡献指南

欢迎提 issue 和 PR。这个项目是纯 Python 的局域网工具，改起来门槛不高，但有几条约定请先看一眼。

## 环境准备

```bash
git clone https://github.com/yuxiaole-bili/yumirror && cd yumirror
python -m venv venv && . venv/bin/activate      # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

要求 **Python 3.10+**。`cryptography` 是必需项：传输加密（TLS）和加密积木都依赖它。

## 跑测试（改完必须全绿）

```bash
python tests/smoke_test.py        # 功能冒烟 50 项（含兼容性静态检查）
python tests/security_test.py     # 安全攻防 37~38 项
python scripts/demo_flows.py      # 真起服务端+客户端跑 4 个备份模板
python scripts/gen_docs.py --check  # 文档是否与引擎同步
```

两套测试都自带环境隔离（临时目录起真进程），跑完自动清理，加 `-k` 可保留现场排查。
退出码非 0 即有失败项，可直接用于 CI。

## 代码地图

```
server/server.py        服务端：TCP 服务、备份存储与版本、Flask 管理面板
client/client.py        客户端：watchdog 监听、同步管线、面板、RPC、流程触发
shared/protocol.py      6 字节头协议 + Connection 封装
shared/tls_util.py      传输层 TLS：自签证书生成、指纹固定（TOFU）
shared/flow_engine.py   流程引擎：BlockRegistry（积木）+ 执行器 + 内置模板
shared/crypto_util.py   AES-256-GCM 分块加密容器（LMENC1）
shared/backup_framework.py  客户端收发流水线
client/templates/*.html 面板与编辑器（Jinja + 原生 JS，无构建步骤）
tests/                  smoke_test.py / security_test.py
scripts/gen_docs.py     从引擎生成 docs/流程积木参考.md
```

## 加一个流程积木（最容易踩的坑）

新增积木**必须同时改三处**，否则要么编辑器里拖不出来，要么保存后引擎不认：

1. `shared/flow_engine.py`：在 `BlockRegistry` 里注册描述符（`type / label / icon / config_schema / description`），
   并在执行器分发处实现 `_exec_xxx`；
2. `client/templates/flow_editor.html`：在 `Object.assign(TYPES, {...})` 里补一条（拖拽式编辑器用）；
3. `client/templates/blocks.html`：在 `R = {...}` 里补一条（积木模式编辑器用）。

`tests/smoke_test.py` 有**覆盖率断言**：引擎里的积木如果没出现在两个编辑器里，测试直接失败——
所以改完跑一遍测试就知道有没有漏。

加了模板还要跑 **`python tests/template_test.py`**：它会起真服务端+真客户端把模板真跑一遍。
新模板如果依赖同伴或外网，请在 `tests/template_test.py` 的 `NEEDS_PEER` / `NEEDS_WEBHOOK`
里登记（否则会被当成失败）。模板文件放在 `shared/flow_templates_extra.py`，
不要直接往 `flow_engine.py` 的大字典里塞。

同理，新增/改名模板要同步 `TEMPLATE_META`（分类 + 描述）与 `TEMPLATE_CATEGORIES`，然后
`python scripts/gen_docs.py` 重新生成参考文档（**不要手写 `docs/流程积木参考.md`**，会被 `--check` 判为不同步）。

## 文案与多语言（i18n）

界面文案统一走 `shared/i18n.py`，**不要**在 HTML/JS 里新写死中文：

1. 加 key 到 `UI['zh-CN']` 与 `UI['en-US']`，两个语言都要写（键必须一致）；
2. 静态 HTML 文案：直接写中文即可——非中文界面下前端会按"中文原文 → 译文"精确匹配替换
   （支持 `🧩 流程编辑器` 这种 emoji 前缀）；也可以给元素打 `data-i18n="key"`、
   `data-i18n-ph="key"`（placeholder）、`data-i18n-title="key"`（title）来精确指定；
3. **JS 动态生成的文案**（`innerHTML`、toast、表格行）要改成 `YM_T('key')`——
   脚本已在每个页面注入 `window.YM_T`；
4. 新增积木/模板时补 `BLOCK_I18N` / `TEMPLATE_I18N` 的英文名，新分类补 `CATEGORY_I18N`；
5. 跑 `python tests/smoke_test.py`：词条奇偶校验、积木/模板覆盖率会告诉你漏了什么。

后端返回给用户的接口消息也要走 `i18n.t(lang, 'key')`（用 `_cur_lang()` 取当前语言），
别写死中文字符串。

## 兼容性红线

- **支持 Python 3.10**：不要用 3.11+ 才有的语法与库，例如
  - f-string 里嵌套同种引号（PEP 701，3.12+）——3.10 会直接语法错误；
  - `tomllib`（3.11+）、`ExceptionGroup`（3.11+）、`itertools.batched`（3.12+）；
  - 注解里的类型名**必须 import**：3.14 延迟求值能跑，3.10 一导入就 `NameError`
    （`tests/smoke_test.py` 的 [0] 段专门静态检查这一点）。
- 只在临时目录里跑测试，不要碰用户真实的 `config.json` / 备份目录。

## 提交与 PR

- 提交信息用什么语言都行，能说清"改了什么、为什么"即可；
- PR 里请说明：动机、影响面、怎么验证的（贴测试输出最好）；
- 涉及安全相关改动（认证、加密、路径处理、流程执行）请额外说明威胁模型；
- 检查清单：
  - [ ] `tests/smoke_test.py` 与 `tests/security_test.py` 全绿
  - [ ] 动了积木/模板 → 已跑 `scripts/gen_docs.py` 并提交生成结果
  - [ ] 没有把 `config.json`、`users.json`、`certs/`、私钥、日志提交进去
  - [ ] 用户可见的行为变化写进了 `CHANGELOG.md`
  - [ ] 新功能配了测试（能在 CI 上跑）

## 报告安全问题

不要开公开 issue 贴利用细节，请看 [SECURITY.md](SECURITY.md)。
