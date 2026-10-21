[app]
title = TradeMatrix
package.name = tradematrix
package.domain = org.trading
source.include_exts = py,png,jpg,kv,atlas,json,ttf
source.include_dir = .
version = 1.0
requirements = python3,kivy==2.3.0,requests,urllib3,certifi,idna,charset-normalizer,pandas,numpy,gTTS
orientation = portrait
osx.python_version = 3
osx.kivy_version = 2.3.0
fullscreen = 0
android.permissions = INTERNET,ACCESS_NETWORK_STATE,RECORD_AUDIO
android.api = 33
android.minapi = 21
android.ndk = 25b
android.archs = arm64-v8a, armeabi-v7a
