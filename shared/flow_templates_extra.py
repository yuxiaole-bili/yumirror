"""补充预设工作流（第二阶段新增的 8 个实用模板）。

单独放一个模块、在 flow_engine 里 update 进 FLOW_TEMPLATES / TEMPLATE_META，
这样新增模板不用去动那个很大的字典，review 也清爽。

全部经过真机执行验证：tests/ 里跑的是"起真服务端 + 真客户端 + 真文件"，
每个模板都要 0 失败（见 scripts/verify_templates.py）。
"""

EXTRA_TEMPLATES = {
    # ── 1. 按类型分类归档：把同步目录里的文件按扩展名归到 归档/图片、文档、其他 ──
    'classify_archive_by_type': {
        'name': '⑫ 按类型分类归档',
        'description': '扫描同步目录，按扩展名把文件归到 归档/图片、归档/文档、归档/其他',
        'steps': [
            {'id': 'c1', 'type': 'scan_dir', 'label': '扫描同步目录', 'config': {
                'folder': '@folder:{{_folder}}', 'pattern': '*', 'recursive': True,
                'result_var': '_files'}},
            {'id': 'c2', 'type': 'log', 'label': '开始分类', 'config': {
                'message': '🗂 开始分类 {{_scan_count}} 个文件'}},
            {'id': 'c3', 'type': 'foreach', 'label': '逐文件判断类型', 'config': {
                'list_var': '_files', 'max_items': 500, 'steps': [
                    {'id': 'c3a', 'type': 'compare', 'label': '是图片吗', 'config': {
                        'left': '{{_item}}', 'op': 'matches',
                        'right': r'\.(png|jpg|jpeg|gif|webp|bmp|svg)$'}},
                    {'id': 'c3b', 'type': 'branch', 'label': '按类型分流', 'config': {
                        'var': '_compare_result',
                        'true_steps': [
                            {'id': 'c3b1', 'type': 'copy_file', 'label': '归到图片', 'config': {
                                'src': '{{_item_path}}',
                                'dst_dir': '@folder:{{_folder}}/归档/图片',
                                'overwrite': True, 'result_var': '_copied'}},
                        ],
                        'false_steps': [
                            {'id': 'c3b2', 'type': 'compare', 'label': '是文档吗', 'config': {
                                'left': '{{_item}}', 'op': 'matches',
                                'right': r'\.(docx?|xlsx?|pptx?|pdf|md|txt|csv)$'}},
                            {'id': 'c3b3', 'type': 'branch', 'label': '文档/其他', 'config': {
                                'var': '_compare_result',
                                'true_steps': [
                                    {'id': 'c3b4', 'type': 'copy_file', 'label': '归到文档', 'config': {
                                        'src': '{{_item_path}}',
                                        'dst_dir': '@folder:{{_folder}}/归档/文档',
                                        'overwrite': True, 'result_var': '_copied'}},
                                ],
                                'false_steps': [
                                    {'id': 'c3b5', 'type': 'copy_file', 'label': '归到其他', 'config': {
                                        'src': '{{_item_path}}',
                                        'dst_dir': '@folder:{{_folder}}/归档/其他',
                                        'overwrite': True, 'result_var': '_copied'}},
                                ]}},
                        ]}},
                ]}},
            {'id': 'c4', 'type': 'log', 'label': '分类完成', 'config': {
                'message': '✅ 分类归档完成，处理 {{_foreach_total}} 个文件'}},
        ],
    },

    # ── 2. 变更审计 CSV：每次变更追加一行（时间/路径/哈希），可直接用 Excel 打开 ──
    'change_audit_csv': {
        'name': '⑬ 变更审计 CSV',
        'description': '每次文件变更往 归档/变更审计.csv 追加一行：时间、相对路径、SHA256',
        'steps': [
            {'id': 'a1', 'type': 'get_file_hash', 'label': '算哈希', 'config': {
                'file_path': '{{_full_path}}', 'result_var': '_local_hash'}},
            {'id': 'a2', 'type': 'write_file', 'label': '追加审计行', 'config': {
                'file_path': '@folder:{{_folder}}/归档/变更审计.csv',
                'content': '{{_timestamp}},{{_relpath}},{{_local_hash}}\n',
                'mode': 'append'}},
            {'id': 'a3', 'type': 'log', 'label': '记录完成', 'config': {
                'message': '🧾 审计已记录: {{_relpath}} · {{_local_hash}}'}},
        ],
    },

    # ── 3. 夜间快照 + 归档：整目录打包 → 上传 → 打版本点 ──
    'nightly_snapshot_rotate': {
        'name': '⑭ 夜间快照归档',
        'description': '把整个同步目录打包成带时间戳的 zip，上传服务端并打一个版本点',
        'steps': [
            {'id': 'n1', 'type': 'archive_zip', 'label': '打包快照', 'config': {
                'src': '@folder:{{_folder}}',
                'zip_path': '{{_client_dir}}/snapshots/快照-{{_folder}}-{{_timestamp}}.zip',
                'result_var': '_zip_path'}},
            {'id': 'n2', 'type': 'upload_to_server', 'label': '上传快照', 'config': {
                'file_path': '{{_zip_path}}',
                'remote_path': 'snapshots/{{_folder}}-{{_timestamp}}.zip',
                'result_var': '_upload_ok'}},
            {'id': 'n3', 'type': 'snapshot_version', 'label': '打版本点', 'config': {
                'file_path': 'snapshots/{{_folder}}-{{_timestamp}}.zip',
                'reason': 'nightly', 'result_var': '_version_id'}},
            {'id': 'n4', 'type': 'log', 'label': '快照完成', 'config': {
                'message': '📦 夜间快照完成: {{_zip_path}} → {{_upload_ok}}'}},
        ],
    },

    # ── 4. 加密归档 + 版本核对：加密后上传，再确认服务端确实留下了版本 ──
    'encrypted_archive_verify': {
        'name': '⑮ 加密归档并核对版本',
        'description': 'AES-256-GCM 加密后上传，再列出服务端版本确认留档成功',
        'steps': [
            {'id': 'e1', 'type': 'encrypt_file', 'label': '加密', 'config': {
                'file_path': '{{_full_path}}',
                'passphrase_secret': 'backup_passphrase',
                'out_path': '@folder:{{_folder}}/vault/{{_relpath}}.enc',
                'delete_source': False, 'result_var': '_enc_path'}},
            {'id': 'e2', 'type': 'upload_to_server', 'label': '上传密文', 'config': {
                'file_path': '{{_enc_path}}', 'remote_path': 'vault/{{_relpath}}.enc',
                'result_var': '_upload_ok'}},
            {'id': 'e3', 'type': 'list_versions', 'label': '列版本', 'config': {
                'file_path': 'vault/{{_relpath}}.enc', 'result_var': '_versions'}},
            {'id': 'e4', 'type': 'compare', 'label': '有版本吗', 'config': {
                'left': '{{_version_count}}', 'op': '>=', 'right': '1'}},
            {'id': 'e5', 'type': 'branch', 'label': '按结果记录', 'config': {
                'var': '_compare_result',
                'true_steps': [
                    {'id': 'e5a', 'type': 'log', 'config': {
                        'message': '🔐 加密归档完成，服务端已有 {{_version_count}} 个版本'}},
                ],
                'false_steps': [
                    {'id': 'e5b', 'type': 'log', 'config': {
                        'message': '⚠ 密文已上传但服务端还没有版本记录（上传={{_upload_ok}}）'}},
                ]}},
        ],
    },

    # ── 5. 大文件先压缩再传：超过 1MB 的先打包再上传，省带宽 ──
    'large_file_compress_upload': {
        'name': '⑯ 大文件压缩后上传',
        'description': '超过 1MB 的文件先打包成 zip 再上传，小文件直接传',
        'steps': [
            {'id': 'l1', 'type': 'get_path_info', 'label': '取文件大小', 'config': {
                'source': 'local', 'file_path': '{{_full_path}}',
                'info_type': 'size', 'result_var': '_size'}},
            {'id': 'l2', 'type': 'compare', 'label': '是否超过 1MB', 'config': {
                'left': '{{_size}}', 'op': '>=', 'right': '1048576'}},
            {'id': 'l3', 'type': 'branch', 'label': '分流上传', 'config': {
                'var': '_compare_result',
                'true_steps': [
                    {'id': 'l3a', 'type': 'archive_zip', 'label': '打包', 'config': {
                        'src': '{{_full_path}}',
                        'zip_path': '{{_client_dir}}/large/大文件-{{_relpath}}-{{_timestamp}}.zip',
                        'result_var': '_zip_path'}},
                    {'id': 'l3b', 'type': 'upload_to_server', 'label': '上传压缩包', 'config': {
                        'file_path': '{{_zip_path}}',
                        'remote_path': 'large/{{_relpath}}.zip',
                        'result_var': '_upload_ok'}},
                    {'id': 'l3c', 'type': 'log', 'config': {
                        'message': '🗜 {{_relpath}} ({{_size}} 字节) 已压缩上传'}},
                ],
                'false_steps': [
                    {'id': 'l3d', 'type': 'upload_to_server', 'label': '直传', 'config': {
                        'file_path': '{{_full_path}}', 'remote_path': '{{_relpath}}',
                        'result_var': '_upload_ok'}},
                    {'id': 'l3e', 'type': 'log', 'config': {
                        'message': '⬆ {{_relpath}} ({{_size}} 字节) 直接上传'}},
                ]}},
        ],
    },

    # ── 6. 同步前预检：目录在读、可写、文件数不为 0，否则告警 ──
    'preflight_guard': {
        'name': '⑰ 同步前预检',
        'description': '检查同步目录是否存在/可读、文件数是否正常，异常就告警（可挂 webhook）',
        'steps': [
            {'id': 'p1', 'type': 'get_path_info', 'label': '统计目录文件数', 'config': {
                'source': 'local', 'file_path': '@folder:{{_folder}}',
                'info_type': 'count', 'result_var': '_count'}},
            {'id': 'p2', 'type': 'compare', 'label': '目录非空吗', 'config': {
                'left': '{{_count}}', 'op': '>', 'right': '0'}},
            {'id': 'p3', 'type': 'branch', 'label': '按预检结果', 'config': {
                'var': '_compare_result',
                'true_steps': [
                    {'id': 'p3a', 'type': 'log', 'config': {
                        'message': '✅ 预检通过：目录内 {{_count}} 个文件'}},
                ],
                'false_steps': [
                    {'id': 'p3b', 'type': 'log', 'config': {
                        'message': '⚠ 预检失败：同步目录为空或不可访问 —— {{_folder}}'}},
                    {'id': 'p3c', 'type': 'http_request', 'label': '发告警', 'config': {
                        'url': 'https://example.com/webhook',
                        'method': 'POST',
                        'headers': '{"Content-Type": "application/json"}',
                        'body': '{"text": "yumirror 预检失败：{{_folder}} 目录异常"}',
                        'result_var': '_alert'}},
                ]}},
        ],
    },

    # ── 7. 全量清单 + 逐文件核对服务端备份 ──
    'weekly_manifest_verify': {
        'name': '⑱ 全量清单并核对备份',
        'description': '逐文件核对服务端备份是否存在/一致，最后生成含 SHA256 的清单并上传留档'
                       '（若 ignore_patterns 排除了某目录，该目录下的文件会被报缺失，属正常）',
        'steps': [
            {'id': 'w3', 'type': 'scan_dir', 'label': '扫描待核对文件', 'config': {
                'folder': '@folder:{{_folder}}', 'pattern': '*', 'recursive': True,
                'result_var': '_files'}},
            {'id': 'w4', 'type': 'foreach', 'label': '逐文件核对', 'config': {
                'list_var': '_files', 'max_items': 500, 'steps': [
                    {'id': 'w4a', 'type': 'verify_backup', 'config': {
                        'file_path': '{{_item_path}}', 'remote_path': '{{_item}}',
                        'result_var': '_verify_ok'}},
                    {'id': 'w4b', 'type': 'branch', 'config': {
                        'var': '_verify_ok',
                        'true_steps': [
                            {'id': 'w4c', 'type': 'log', 'config': {
                                'message': '✅ [{{_index}}] {{_item}} 备份一致'}},
                        ],
                        'false_steps': [
                            {'id': 'w4d', 'type': 'log', 'config': {
                                'message': '❌ [{{_index}}] {{_item}} 服务端备份缺失或哈希不符'}},
                        ]}},
                ]}},
            {'id': 'w1', 'type': 'manifest', 'label': '生成清单', 'config': {
                'dir': '@folder:{{_folder}}', 'pattern': '*', 'format': 'json',
                'out_path': '{{_client_dir}}/manifests/清单-{{_folder}}-{{_timestamp}}.json',
                'result_var': '_manifest_path'}},
            {'id': 'w2', 'type': 'upload_to_server', 'label': '上传清单', 'config': {
                'file_path': '{{_manifest_path}}',
                'remote_path': 'manifests/清单-{{_timestamp}}.json',
                'result_var': '_upload_ok'}},
            {'id': 'w5', 'type': 'log', 'label': '汇总', 'config': {
                'message': '📊 全量核对结束：共 {{_foreach_total}} 个文件，逐行结果见上（一致 / 缺失）'}},
        ],
    },

    # ── 8. 变更留档到 vault：每次变更都在 vault 留一份，方便回退 ──
    'vault_on_change': {
        'name': '⑲ 变更留档到 vault',
        'description': '每次变更把当前内容按时间戳存一份到 vault 目录，并打一个版本点',
        'steps': [
            {'id': 'v1', 'type': 'copy_file', 'label': '留档到 vault', 'config': {
                'src': '{{_full_path}}',
                'dst_dir': '@folder:{{_folder}}/vault/{{_relpath}}.{{_timestamp}}',
                'overwrite': True, 'result_var': '_vault_ok'}},
            {'id': 'v2', 'type': 'snapshot_version', 'label': '打版本点', 'config': {
                'file_path': '{{_relpath}}', 'reason': 'vault',
                'result_var': '_version_id'}},
            {'id': 'v3', 'type': 'log', 'label': '留档完成', 'config': {
                'message': '🗄 已留档: {{_relpath}} → vault（版本 {{_version_id}}）'}},
        ],
    },
}

EXTRA_META = {
    'classify_archive_by_type': ['备份', '扫描同步目录，按扩展名把文件归到 归档/图片、归档/文档、归档/其他'],
    'change_audit_csv': ['巡检', '每次文件变更往 归档/变更审计.csv 追加一行：时间、相对路径、SHA256'],
    'nightly_snapshot_rotate': ['版本', '把整个同步目录打包成带时间戳的 zip，上传服务端并打一个版本点'],
    'encrypted_archive_verify': ['版本', 'AES-256-GCM 加密后上传，再列出服务端版本确认留档成功'],
    'large_file_compress_upload': ['备份', '超过 1MB 的文件先打包成 zip 再上传，小文件直接传'],
    'preflight_guard': ['系统', '检查同步目录是否存在/可读、文件数是否正常，异常就告警（可挂 webhook）'],
    'weekly_manifest_verify': ['校验', '生成目录清单（含 SHA256）上传留档，再逐文件核对服务端备份是否存在'],
    'vault_on_change': ['备份', '每次变更把当前内容按时间戳存一份到 vault 目录，并打一个版本点'],
}
