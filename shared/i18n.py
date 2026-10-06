"""多语言（i18n）：面板文案、接口消息、流程积木与模板的英文名。

设计：
- 词条集中在 UI 字典里，`zh-CN` 与 `en-US` 必须**键完全一致**（tests/smoke_test.py 有奇偶校验，
  少翻一条就测试失败，防止漏翻）。
- 积木/模板的英文名放在 BLOCK_I18N / TEMPLATE_I18N，键取自引擎（type / 模板 key），
  引擎新增积木后如果没补英文，同样会被测试抓出来。
- 前端不写死翻译：页面元素打 `data-i18n="key"`（`data-i18n-ph` 管 placeholder、
  `data-i18n-title` 管 title 属性），由 frontend_js() 注入的脚本拉 `/api/i18n` 后套用；
  语言选择存在 cookie `yumirror_lang`，没存时用服务端给的默认值（配置 language 或 Accept-Language）。

添加文案的规矩：先加 key，两个语言都写，再跑测试。
"""

LANGS = {'zh-CN': '简体中文', 'en-US': 'English'}
DEFAULT_LANG = 'zh-CN'
COOKIE_NAME = 'yumirror_lang'

UI = {
    'zh-CN': {
        # 品牌与导航
        'app_name': 'yumirror',
        'app_tagline': '局域网文件镜像与集中备份',
        'role_client': '客户端',
        'role_server': '服务端',
        'nav_home': '首页',
        'nav_files': '文件管理',
        'nav_flows': '方案管理',
        'nav_editor': '编辑器',
        'nav_workshop': '工坊',
        'nav_flow_editor': '流程编辑器',
        'nav_blocks': '积木模式',
        'nav_back': '← 面板',
        # 按钮
        'btn_login': '登录',
        'btn_logout': '登出',
        'btn_register': '注册',
        'btn_new': '新建',
        'btn_open': '打开',
        'btn_save': '保存',
        'btn_test': '测试',
        'btn_share': '分享',
        'btn_install': '安装',
        'btn_install_flow': '安装此流程',
        'btn_refresh': '刷新',
        'btn_refresh_all': '全部刷新',
        'btn_refresh_devices': '刷新设备',
        'btn_reconnect': '重连',
        'btn_full_sync': '全面同步',
        'btn_upload': '上传',
        'btn_download': '下载',
        'btn_delete': '删除',
        'btn_rename': '重命名',
        'btn_preview': '预览',
        'btn_new_file': '新建文件',
        'btn_new_folder': '新建文件夹',
        'btn_close': '关闭',
        'btn_cancel': '取消',
        'btn_ok': '确定',
        'btn_save_triggers': '保存触发器',
        'btn_add_trigger': '+ 添加触发',
        'btn_run': '运行',
        'btn_play': '播放',
        # 状态与统计
        'st_connected': '已连接',
        'st_disconnected': '未连接',
        'st_peers': '同伴',
        'st_received': '接收',
        'st_sent': '发送',
        'st_conflicts': '冲突',
        'st_flows': '流程',
        'st_tls_on': 'TLS 已加密',
        'st_tls_off': '明文传输',
        'st_watching': '监听中',
        'st_idle': '待机',
        # 区块标题
        'sec_devices': '设备管理',
        'sec_sync_folders': '同步文件夹',
        'sec_preview': '文件预览',
        'sec_peers_online': '在线同伴',
        'sec_sync_groups': '同步组配置',
        'sec_triggers': '触发器配置',
        'sec_manual': '手动执行',
        'sec_events': '实时事件',
        'sec_backups': '备份',
        'sec_users': '用户',
        'sec_shared_flows': '共享方案',
        'sec_stats': '服务端状态',
        # 表头与空态
        'col_name': '名称',
        'col_size': '大小',
        'col_mtime': '修改时间',
        'col_actions': '操作',
        'col_hash': '哈希',
        'col_status': '状态',
        'empty_none': '暂无数据',
        'empty_no_peers': '暂无同伴在线',
        'empty_no_flows': '暂无流程执行记录',
        'empty_pick_file': '点击左侧文件查看预览',
        'empty_no_devices': '该账户暂无绑定设备',
        # 登录
        'login_username': '用户名',
        'login_password': '密码',
        'login_hint': '默认没有账户，注册一个即可（第一个注册的自动是管理员）',
        'err_wrong_password': '用户名或密码错误',
        'err_too_many_attempts': '登录失败次数过多，请稍后再试',
        # 语言
        'lang_label': '语言',
        # 接口消息
        'err_not_logged_in': '未登录：请先登录',
        'err_share_needs_account': '未登录：共享方案需要服务端账户（客户端请在 config.json 配置 account 后登录）',
        'err_group_key': '组密钥错误或缺失',
        'err_group_full': '组已满',
        'err_file_missing': '文件不存在',
        'err_dir_missing': '目录不存在',
        'err_bad_path': '越界访问：路径不在同步目录内',
        'err_tls_fingerprint': '服务端证书指纹不匹配，已拒绝连接（可能是中间人；若确实换了证书，清空 config 里的 tls.fingerprint 后重连）',
        'err_tls_missing': '已启用 TLS 但缺少 cryptography，请先安装：pip install cryptography',
        'msg_tls_pinned': '已固定服务端证书指纹',
        'msg_tls_connected': '传输加密：已启用 (TLS 1.2+)',
        'msg_tls_off': '传输加密：已关闭（明文，仅建议调试）',
        # 校验与提示
        'msg_upload_ok': '已上传',
        'msg_verify_ok': '备份完成且校验通过',
        'msg_verify_fail': '校验失败',
        'msg_flow_done': '流程执行完毕',
        'msg_flow_failed': '流程执行失败',
        'msg_saved': '已保存',
        'msg_deleted': '已删除',
    },
    'en-US': {
        'app_name': 'yumirror',
        'app_tagline': 'LAN file mirroring & central backup',
        'role_client': 'Client',
        'role_server': 'Server',
        'nav_home': 'Home',
        'nav_files': 'Files',
        'nav_flows': 'Flows',
        'nav_editor': 'Editor',
        'nav_workshop': 'Workshop',
        'nav_flow_editor': 'Flow editor',
        'nav_blocks': 'Block mode',
        'nav_back': '← Panel',
        'btn_login': 'Sign in',
        'btn_logout': 'Sign out',
        'btn_register': 'Register',
        'btn_new': 'New',
        'btn_open': 'Open',
        'btn_save': 'Save',
        'btn_test': 'Test',
        'btn_share': 'Share',
        'btn_install': 'Install',
        'btn_install_flow': 'Install this flow',
        'btn_refresh': 'Refresh',
        'btn_refresh_all': 'Refresh all',
        'btn_refresh_devices': 'Refresh devices',
        'btn_reconnect': 'Reconnect',
        'btn_full_sync': 'Full sync',
        'btn_upload': 'Upload',
        'btn_download': 'Download',
        'btn_delete': 'Delete',
        'btn_rename': 'Rename',
        'btn_preview': 'Preview',
        'btn_new_file': 'New file',
        'btn_new_folder': 'New folder',
        'btn_close': 'Close',
        'btn_cancel': 'Cancel',
        'btn_ok': 'OK',
        'btn_save_triggers': 'Save triggers',
        'btn_add_trigger': '+ Add trigger',
        'btn_run': 'Run',
        'btn_play': 'Play',
        'st_connected': 'Connected',
        'st_disconnected': 'Disconnected',
        'st_peers': 'Peers',
        'st_received': 'Received',
        'st_sent': 'Sent',
        'st_conflicts': 'Conflicts',
        'st_flows': 'Flows',
        'st_tls_on': 'TLS encrypted',
        'st_tls_off': 'Plaintext',
        'st_watching': 'Watching',
        'st_idle': 'Idle',
        'sec_devices': 'Devices',
        'sec_sync_folders': 'Sync folders',
        'sec_preview': 'Preview',
        'sec_peers_online': 'Online peers',
        'sec_sync_groups': 'Sync groups',
        'sec_triggers': 'Triggers',
        'sec_manual': 'Manual runs',
        'sec_events': 'Live events',
        'sec_backups': 'Backups',
        'sec_users': 'Users',
        'sec_shared_flows': 'Shared flows',
        'sec_stats': 'Server status',
        'col_name': 'Name',
        'col_size': 'Size',
        'col_mtime': 'Modified',
        'col_actions': 'Actions',
        'col_hash': 'Hash',
        'col_status': 'Status',
        'empty_none': 'No data',
        'empty_no_peers': 'No peers online',
        'empty_no_flows': 'No flow runs yet',
        'empty_pick_file': 'Pick a file on the left to preview',
        'empty_no_devices': 'No devices bound to this account',
        'login_username': 'Username',
        'login_password': 'Password',
        'login_hint': 'No account yet — register one (the first account becomes admin)',
        'err_wrong_password': 'Wrong username or password',
        'err_too_many_attempts': 'Too many failed attempts, please try again later',
        'lang_label': 'Language',
        'err_not_logged_in': 'Not signed in',
        'err_share_needs_account': 'Sign-in required to share flows (set "account" in client config.json)',
        'err_group_key': 'Wrong or missing group key',
        'err_group_full': 'Group is full',
        'err_file_missing': 'File not found',
        'err_dir_missing': 'Directory not found',
        'err_bad_path': 'Path outside the sync folder',
        'err_tls_fingerprint': 'Server certificate fingerprint mismatch — connection refused '
                              '(possible MITM; if the certificate was rotated, clear tls.fingerprint and retry)',
        'err_tls_missing': 'TLS is enabled but cryptography is missing: pip install cryptography',
        'msg_tls_pinned': 'Pinned server certificate fingerprint',
        'msg_tls_connected': 'Transport encryption: enabled (TLS 1.2+)',
        'msg_tls_off': 'Transport encryption: disabled (plaintext, debug only)',
        'msg_upload_ok': 'Uploaded',
        'msg_verify_ok': 'Backup finished and verified',
        'msg_verify_fail': 'Verification failed',
        'msg_flow_done': 'Flow finished',
        'msg_flow_failed': 'Flow failed',
        'msg_saved': 'Saved',
        'msg_deleted': 'Deleted',
    },
}

# 模板分类
CATEGORY_I18N = {
    '备份': 'Backup', '版本': 'Version', '校验': 'Verify', '同步': 'Sync',
    '清理': 'Cleanup', '通知': 'Notify', '巡检': 'Inspect', '系统': 'System',
}

# 流程积木英文名（键 = 引擎里的 type）
BLOCK_I18N = {
    'archive_zip': ('Pack as ZIP', 'Pack files or a directory into a zip (appends if it exists)'),
    'backup_to_server': ('Backup to server', 'Check whether the file already has a server-side backup'),
    'branch': ('If / else', 'Run different sub-steps based on a variable'),
    'cleanup_old': ('Clean old files', 'Delete files older than N days (dry-run by default)'),
    'compare': ('Compare', 'Compare two values and store the boolean result'),
    'copy_file': ('Copy / archive file', 'Copy the file into an archive directory (@folder:name supported)'),
    'decrypt_file': ('Decrypt file', 'Decrypt a .enc file (tampering raises an error)'),
    'delete_file': ('Delete file', 'Delete a local file or empty directory (confined to the sync folder)'),
    'encrypt_file': ('Encrypt file', 'AES-256-GCM chunked encryption, passphrase or key file; writes .enc'),
    'fetch_version': ('Fetch old version', 'Fetch a stored version back to this machine and verify its SHA256'),
    'foreach': ('For each', 'Run sub-steps for every item of a list variable (pair with Scan directory)'),
    'get_file_hash': ('Hash local file', 'Compute the SHA256 of a local file'),
    'get_path_info': ('Path info', 'Query existence/size/mtime on local, server or peer'),
    'get_peer_hash': ('Hash on peer', 'Ask a peer for the SHA256 of a file'),
    'get_server_hash': ('Hash on server', 'Ask the server for the SHA256 of a backup'),
    'http_request': ('HTTP request', 'Send an HTTP request (webhooks, notifications)'),
    'list_versions': ('List versions', 'List stored versions plus the current one for a file'),
    'log': ('Log', 'Write a line to the run log'),
    'make_key_file': ('Generate key file', 'Generate a 32-byte random key file (mode 0600)'),
    'manifest': ('Build manifest', 'Scan a directory and build a manifest (name/size/mtime/SHA256)'),
    'mirror_from_peer': ('Mirror from peer', 'Ask a peer to exchange snapshots and mirror both ways'),
    'restore_version': ('Restore version', 'Roll the server backup back to latest / latest-2 / a version id; '
                                           'the current content is archived first, so it can be undone'),
    'run_command': ('Run command', 'Run a local command (disabled unless allow_command_block is true)'),
    'scan_dir': ('Scan directory', 'List files in a directory into a variable'),
    'set_var': ('Set variable', 'Assign a value to a flow variable'),
    'sleep': ('Wait', 'Pause the flow for a while'),
    'snapshot_version': ('Snapshot version', 'Archive the current server backup as a version mark'),
    'upload_to_server': ('Upload to server', 'Push the file into the server backup store (not just hash check)'),
    'verify_backup': ('Verify backup', 'Compare local SHA256 with the server backup, result in _verify_ok'),
    'write_file': ('Write file', 'Write text content into a file'),
}

# 模板英文名（键 = 模板 key）
TEMPLATE_I18N = {
    'backup_on_change': ('① Backup on change', 'Upload to server and verify the hash on every change'),
    'backup_zip_daily': ('② Daily zip archive', 'Zip the whole folder and upload it as an archive'),
    'bulk_backup_folder': ('③ Bulk folder backup', 'Upload file by file — good for the first seeding'),
    'backup_manifest_report': ('④ Backup manifest report', 'Build a manifest with SHA256 and upload it'),
    'encrypted_backup': ('⑨ Encrypted backup', 'Encrypt before upload — the server only stores ciphertext'),
    'full_pipeline': ('Full pipeline', 'End-to-end example: hash / path / server / HTTP'),
    'auto_backup': ('Auto backup to server', 'Backup on change, compared by hash'),
    'rollback_latest': ('⑩ Roll back one version', 'Roll back to the previous version and fetch it locally'),
    'version_audit': ('⑪ Version audit', 'Check how many versions the server keeps per file'),
    'three_way_guard': ('⑦ Three-way guard', 'Keep local / server / peer consistent, alert on drift'),
    'hash_three_way': ('Three-way hash check', 'Compare hashes across local, server and peer'),
    'hash_check': ('Hash check', 'Compare hashes in parallel across local, server and peer'),
    'backup_verify': ('Backup verify', 'Verify every replica by hash after backup'),
    'multi_folder_sync': ('Multi-folder sync', 'Scan several folders in parallel'),
    'manual_mirror': ('Manual mirror', 'Trigger a peer mirror by hand'),
    'passive_sync': ('Passive sync', 'Record changes without pushing'),
    'mirror_consistency': ('Mirror consistency', 'Compare mirror state between peers'),
    'backup_retention': ('⑥ Archive rotation', 'Rotate and clean the archive (dry-run by default)'),
    'cleanup_tmp': ('Temp file cleanup', 'Remove temporary files'),
    'file_age_cleanup': ('Old file cleanup', 'Delete files by age'),
    'cleanup_scan_report': ('Cleanup report', 'Scan, clean and write a report'),
    'webhook_notify': ('Webhook notify', 'Push a webhook when a file changes'),
    'backup_then_notify': ('⑤ Backup and notify', 'Notify after backup, alert on failure too'),
    'webhook_broadcast': ('Multi-target notify', 'Broadcast to several endpoints'),
    'large_file_alert': ('Large file alert', 'Alert when a big file shows up'),
    'backup_health_check': ('⑧ Backup health check', 'Detect missing server-side backups per file'),
    'peer_status_probe': ('Peer status probe', 'Report which peers are online'),
    'sync_audit_log': ('Sync audit log', 'Write an audit line for every sync'),
    'backup_health': ('Backup health', 'Check existence / size / hash of backups'),
    'parallel_search': ('Parallel search', 'Multi-lane parallel search example'),
    'folder_index_builder': ('Folder index', 'Build a folder index'),
    'command_inspection': ('Command inspection', 'Run a command and inspect its output'),
    'classify_archive_by_type': ('⑫ Archive by type', 'Scan the sync folder and sort files into archive/images, archive/docs, archive/other'),
    'change_audit_csv': ('⑬ Change audit CSV', 'Append one line per change (time, relative path, SHA256) to archive/audit.csv'),
    'nightly_snapshot_rotate': ('⑭ Nightly snapshot', 'Zip the whole sync folder with a timestamp, upload it and mark a version'),
    'encrypted_archive_verify': ('⑮ Encrypted archive + version check', 'Encrypt with AES-256-GCM, upload, then confirm the server kept a version'),
    'large_file_compress_upload': ('⑯ Compress large files', 'Files over 1 MB are zipped before upload, small ones go straight through'),
    'preflight_guard': ('⑰ Pre-flight check', 'Verify the sync folder exists, is readable and not empty — alert otherwise'),
    'weekly_manifest_verify': ('⑱ Manifest + verify all', 'Build a manifest with SHA256, upload it, then verify every server-side backup'),
    'vault_on_change': ('⑲ Vault every change', 'Keep a timestamped copy of every change in a vault folder and mark a version'),
}


def normalize(lang):
    """把 en / en_US / EN-us / zh 等写法归一到受支持的语言代码"""
    if not lang:
        return DEFAULT_LANG
    s = str(lang).strip().replace('_', '-').lower()
    if s in ('zh', 'zh-cn', 'zh-hans', 'cn', 'chinese', 'zh-hans-cn'):
        return 'zh-CN'
    if s in ('en', 'en-us', 'en-gb', 'english'):
        return 'en-US'
    for code in LANGS:
        if code.lower() == s:
            return code
    return DEFAULT_LANG


def negotiate(accept_language, configured=''):
    """优先用配置里的语言；没配就看浏览器的 Accept-Language"""
    if configured:
        return normalize(configured)
    first = (accept_language or '').split(',')[0].strip()
    if first:
        return normalize(first)
    return DEFAULT_LANG


def t(lang, key, **kw):
    """取词条；缺词条时回退到默认语言，再不行就返回 key 本身"""
    code = normalize(lang)
    table = UI.get(code) or {}
    text = table.get(key)
    if text is None:
        text = (UI.get(DEFAULT_LANG) or {}).get(key, key)
    if kw:
        try:
            text = text.format(**kw)
        except Exception:
            pass
    return text


def _text_map(from_lang, to_lang):
    """{源语言原文: 目标语言译文}，供前端按"完全匹配的文本节点"做兜底翻译。

    这样静态文案不必逐个给元素打 data-i18n 也能翻译；只替换与词条完全一致的文本，
    不会误伤文件名、日志这类动态内容。"""
    src = UI.get(from_lang) or {}
    dst = UI.get(to_lang) or {}
    out = {}
    for k, v in src.items():
        w = dst.get(k)
        if w and w != v and isinstance(v, str) and v.strip():
            out[v] = w
    return out


def table(lang):
    """给前端用的完整语言包"""
    code = normalize(lang)
    return {
        'lang': code,
        'default': DEFAULT_LANG,
        'langs': dict(LANGS),
        'strings': dict(UI.get(code) or UI[DEFAULT_LANG]),
        # 非中文界面时，附带"中文原文 → 译文"的兜底映射
        'text_map': _text_map(DEFAULT_LANG, code) if code != DEFAULT_LANG else {},
        'categories': dict(CATEGORY_I18N),
        'blocks': {k: {'label': v[0], 'description': v[1]} for k, v in BLOCK_I18N.items()},
        'templates': {k: {'name': v[0], 'description': v[1]} for k, v in TEMPLATE_I18N.items()},
    }


def frontend_js(default_lang=DEFAULT_LANG):
    """注入到面板 HTML 里的小脚本：拉语言包并套用到 data-i18n 元素，附带语言切换器"""
    return r"""<script>
(function(){
  var COOKIE='%s', DEF='%s';
  function readCookie(){var m=document.cookie.match(new RegExp('(^|; )'+COOKIE+'=([^;]+)'));return m?decodeURIComponent(m[2]):'';}
  function writeCookie(v){document.cookie=COOKIE+'='+encodeURIComponent(v)+';path=/;max-age=31536000;SameSite=Lax';}
  function apply(strings){
    var i,el,key;
    var nodes=document.querySelectorAll('[data-i18n]');
    for(i=0;i<nodes.length;i++){el=nodes[i];key=el.getAttribute('data-i18n');
      if(strings[key]!==undefined){el.textContent=strings[key];}}
    nodes=document.querySelectorAll('[data-i18n-ph]');
    for(i=0;i<nodes.length;i++){el=nodes[i];key=el.getAttribute('data-i18n-ph');
      if(strings[key]!==undefined){el.setAttribute('placeholder',strings[key]);}}
    nodes=document.querySelectorAll('[data-i18n-title]');
    for(i=0;i<nodes.length;i++){el=nodes[i];key=el.getAttribute('data-i18n-title');
      if(strings[key]!==undefined){el.setAttribute('title',strings[key]);}}
    nodes=document.querySelectorAll('select[data-lang-switcher]');
    for(i=0;i<nodes.length;i++){el=nodes[i];if(el.value!==window.YM_LANG)el.value=window.YM_LANG;}
  }
  function matchKey(s,map){
    if(map[s]!==undefined)return s;
    var keys=window.YM_KEYS||[];
    for(var i=0;i<keys.length;i++){
      var k=keys[i];
      if(s.length>k.length&&s.slice(-k.length)===k){
        var pre=s.slice(0,s.length-k.length);
        if(!/[\u4e00-\u9fff]/.test(pre))return k;   /* 允许 "🧩 流程编辑器" 这类 emoji 前缀 */
      }
    }
    return null;
  }
  function applyByText(map){
    if(!map)return;
    var SKIP={SCRIPT:1,STYLE:1,CODE:1,PRE:1,TEXTAREA:1};
    var walker=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT,null,false),n;
    while((n=walker.nextNode())){
      var p=n.parentNode;if(!p||SKIP[p.nodeName])continue;
      var t=n.nodeValue,s=t.replace(/^\s+|\s+$/g,'');
      if(!s)continue;
      var hit=matchKey(s,map);
      if(hit!==null)n.nodeValue=t.replace(hit,map[hit]);
    }
    var els=document.body.querySelectorAll('[title],[placeholder]');
    for(var i=0;i<els.length;i++){
      var a=['title','placeholder'];
      for(var j=0;j<a.length;j++){
        var v=els[i].getAttribute(a[j]);
        if(!v)continue;
        var k2=matchKey(v.replace(/^\s+|\s+$/g,''),map);
        if(k2!==null)els[i].setAttribute(a[j],v.replace(k2,map[k2]));
      }
    }
  }
  window.YM_T=function(k){return (window.YM_STRINGS&&window.YM_STRINGS[k])||k;};
  function buildSwitcher(langs){
    if(document.getElementById('ymLangBox'))return;
    var box=document.createElement('div');
    box.id='ymLangBox';
    box.style.cssText='position:fixed;right:14px;bottom:14px;z-index:99999;display:flex;'+
      'align-items:center;gap:6px;background:#0f172ae6;border:1px solid #2d3e5a;'+
      'border-radius:999px;padding:4px 10px;font:12px/1.6 system-ui,sans-serif;color:#94a3b8';
    var sel=document.createElement('select');
    sel.setAttribute('data-lang-switcher','1');
    sel.style.cssText='background:transparent;border:0;color:#e2e8f0;font:12px system-ui;outline:none;cursor:pointer';
    for(var code in langs){var o=document.createElement('option');o.value=code;
      o.textContent=langs[code];if(code===window.YM_LANG)o.selected=true;o.style.color='#0f172a';sel.appendChild(o);}
    sel.onchange=function(){writeCookie(sel.value);location.reload();};
    box.appendChild(document.createTextNode('🌐'));
    box.appendChild(sel);
    document.body.appendChild(box);
  }
  var lang=readCookie()||DEF;
  window.YM_LANG=lang;
  fetch('/api/i18n?lang='+encodeURIComponent(lang)).then(function(r){return r.json();}).then(function(d){
    window.YM_LANG=d.lang||DEF;window.YM_STRINGS=d.strings||{};
    window.YM_KEYS=Object.keys(d.text_map||{}).sort(function(a,b){return b.length-a.length;});
    document.documentElement.lang=window.YM_LANG;
    apply(window.YM_STRINGS);
    if(d.lang!=='zh-CN')applyByText(d.text_map);
    buildSwitcher(d.langs||{});
  }).catch(function(){});
})();
</script>""" % (COOKIE_NAME, default_lang)
