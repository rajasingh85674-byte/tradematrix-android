[app]
title = Simons Quant Terminal
package.name = simonsquant
package.domain = org.quant
source.dir = .
source.include_exts = py,png,jpg,kv,atlas,json
source.exclude_dirs = .github,bin,.buildozer,__pycache__,venv
version = 1.0.0

# ccxt and pandas-ta are intentionally NOT listed: the engine falls back to the
# Binance REST API (requests) and computes indicators natively with pandas/numpy.
requirements = python3,kivy==2.3.0,numpy,pandas,requests,urllib3,idna,charset-normalizer,certifi

orientation = portrait
fullscreen = 0

android.permissions = INTERNET,ACCESS_NETWORK_STATE
android.api = 33
android.minapi = 24
android.ndk = 25b
android.ndk_api = 24
android.archs = arm64-v8a
android.accept_sdk_license = True
android.enable_androidx = True
android.allow_backup = True

# Pinned python-for-android release that matches NDK 25b + Kivy 2.3.0
p4a.branch = v2024.01.21

[buildozer]
log_level = 2
warn_on_root = 1
