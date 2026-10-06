"""yumirror v3 - 共享模块

包含：TCP 协议封装、同步核心、备份流水线、流程引擎、SSH 隧道工具。
服务端与客户端都以 `from shared.xxx import ...` 的方式引用本包，
因此本目录必须位于 server/ 与 client/ 的上一级。
"""

__version__ = '3.3.0-alpha1'
