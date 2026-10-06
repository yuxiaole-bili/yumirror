#!/usr/bin/env python3
"""从流程引擎自动生成积木/模板参考文档。

    python scripts/gen_docs.py            # 写入 docs/流程积木参考.md
    python scripts/gen_docs.py --check    # 只校验文档是否与代码同步（CI 用）

这样文档不会和引擎脱节：积木增删、参数改名都会体现在生成结果里。
"""
import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from shared.flow_engine import (FLOW_TEMPLATES, TEMPLATE_CATEGORIES,  # noqa: E402
                                TEMPLATE_META, BlockRegistry, list_templates)
from shared.i18n import BLOCK_I18N, CATEGORY_I18N, TEMPLATE_I18N  # noqa: E402

OUT = os.path.join(ROOT, 'docs', '流程积木参考.md')

TYPE_HINT = {
    'string': '文本', 'text': '文本', 'bool': '开关', 'int': '整数',
    'num': '数字', 'sel': '下拉', 'list': '列表', 'json': 'JSON', 'secret': '密钥名',
}


def blocks_table():
    rows = []
    for t in sorted(BlockRegistry.all()):
        d = BlockRegistry.get(t)
        params = []
        for name, spec in (d.get('config_schema') or {}).items():
            kind = TYPE_HINT.get(str(spec.get('type', '')), str(spec.get('type', '')))
            default = spec.get('default', '')
            if isinstance(default, str) and len(default) > 24:
                default = default[:21] + '…'
            params.append(f'`{name}`' + (f'={default!r}' if default != '' else '') + f'（{kind}）')
        en = BLOCK_I18N.get(t, ('', ''))
        rows.append('| {icon} {label} | `{type}` | {en} | {params} | {desc} |'.format(
            icon=d.get('icon', ''), label=d.get('label', t), type=t, en=en[0] or '—',
            params='<br>'.join(params) or '—',
            desc=(d.get('description') or '').replace('\n', ' ')))
    return ('| 积木 | type | English | 参数（默认值 / 类型） | 说明 |\n|---|---|---|---|---|\n'
            + '\n'.join(rows))


def template_catalog():
    out = []
    tpls = list_templates()
    for cat in TEMPLATE_CATEGORIES:
        items = [t for t in tpls if t['category'] == cat]
        if not items:
            continue
        out.append(f'### {cat} / {CATEGORY_I18N.get(cat, cat)}（{len(items)} 个）\n')
        out.append('| 模板 | key | English | 步骤 | 积木 | 说明 |')
        out.append('|---|---|---|---|---|---|')
        for t in items:
            row = dict(t)
            row['en'] = TEMPLATE_I18N.get(t['key'], ('', ''))[0]
            out.append('| {name} | `{key}` | {en} | {steps} | {blocks} | {description} |'.format(**row))
        out.append('')
    return '\n'.join(out)


def template_details():
    out = []
    tpls = {t['key']: t for t in list_templates()}
    for cat in TEMPLATE_CATEGORIES:
        keys = [k for k in FLOW_TEMPLATES if TEMPLATE_META.get(k, [''])[0] == cat]
        if not keys:
            continue
        out.append(f'### {cat}\n')
        for k in keys:
            meta = TEMPLATE_META.get(k, ['', ''])
            t = tpls.get(k, {})
            en_name, en_desc = TEMPLATE_I18N.get(k, ('', ''))
            out.append(f"#### {t.get('name', k)}　`{k}`\n")
            out.append(f"{meta[1]}" + (f"　·　**{en_name}** — {en_desc}" if en_name else '') + "\n")
            flow = FLOW_TEMPLATES[k]
            for q in flow.get('queues', []):
                label = q.get('label', q.get('id', ''))
                out.append('- **车道 %s**（%d 步）' % (label, len(q.get('steps', []))))
                for s in q.get('steps', []):
                    bt = s.get('type', '?')
                    bd = BlockRegistry.get(bt) or {}
                    params = s.get('config') or {}
                    shown = ', '.join('`%s`=%r' % (a, b) for a, b in list(params.items())[:4])
                    name = s.get('label') or bd.get('label', bt)
                    line = '  - %s %s　`%s`' % (bd.get('icon', ''), name, bt)
                    if shown:
                        line += '　' + shown
                    out.append(line)
            if flow.get('merge_at'):
                out.append(f'- 合并点：`{flow["merge_at"]}`')
            out.append('')
    return '\n'.join(out)


def build():
    n_blocks = len(list(BlockRegistry.all()))
    tpls = list_templates()
    return f"""# 流程积木与模板参考

> 本文件由 `python scripts/gen_docs.py` 从 `shared/flow_engine.py` **自动生成**，请勿手工编辑。
> CI 里可以跑 `python scripts/gen_docs.py --check` 校验文档是否与代码同步。

当前共 **{n_blocks} 个积木**、**{len(tpls)} 个内置模板**，分 {len(TEMPLATE_CATEGORIES)} 类：
{' / '.join(TEMPLATE_CATEGORIES)}。

编辑器入口：客户端面板 → 流程编辑器（`/editor`，拖拽式）或积木模式（`/blocks`）。

## 一、积木总表

{blocks_table()}

## 二、模板目录

{template_catalog()}

## 三、模板步骤明细

{template_details()}
## 四、模板里可用的变量

| 变量 | 含义 |
|---|---|
| `{{{{_full_path}}}}` | 触发文件在本地的绝对路径 |
| `{{{{_relpath}}}}` | 相对同步目录的路径（服务端备份就用它） |
| `{{{{_folder}}}}` | 触发文件所在同步文件夹的**名字** |
| `{{{{_client_dir}}}}` | 客户端程序目录 |
| `{{{{_timestamp}}}}` | 当前整数秒时间戳（可用于文件名/版本标记） |
| `{{{{_local_hash}}}}` `{{{{_server_hash}}}}` `{{{{_peer_hash}}}}` | 各步骤写入的哈希结果变量 |
| `@folder:名字` | 取该同步文件夹在**本机**的绝对路径 |

密钥类参数（`passphrase_secret`）填的是**配置项名字**，不是口令本身：
口令放在 `client/config.json` 的 `backup_passphrase` / `backup_key_file`，积木通过名字去取，
不会出现在流程文件、日志或接口返回里。
"""


def main():
    text = build()
    if '--check' in sys.argv:
        if not os.path.isfile(OUT):
            print('缺少 %s，请运行 python scripts/gen_docs.py' % OUT)
            return 1
        with io.open(OUT, 'r', encoding='utf-8') as f:
            cur = f.read()
        if cur != text:
            print('文档与代码不同步：请运行 python scripts/gen_docs.py 后提交')
            return 1
        print('文档与代码一致 ✓')
        return 0
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with io.open(OUT, 'w', encoding='utf-8', newline='\n') as f:
        f.write(text)
    print('已生成 %s（%d 字节）' % (OUT, len(text.encode('utf-8'))))
    return 0


if __name__ == '__main__':
    sys.exit(main())
