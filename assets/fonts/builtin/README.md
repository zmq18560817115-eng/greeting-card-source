# 内置中文字体

字体来自 [Noto CJK 官方项目](https://github.com/notofonts/noto-cjk)，使用未经修改的简体中文 Regular OTF 文件。
上游版本固定于 `f8d157532fbfaeda587e826d4cd5b21a49186f7c`。两套字体的完整 SIL Open Font License 1.1 随字体保存在对应目录的 `LICENSE.txt`。

| 字体 | 上游路径 | SHA-256 |
|---|---|---|
| Noto 黑体 | Sans/OTF/SimplifiedChinese/NotoSansCJKsc-Regular.otf | 2c76254f6fc379fddfce0a7e84fb5385bb135d3e399294f6eeb6680d0365b74b |
| Noto 宋体 | Serif/OTF/SimplifiedChinese/NotoSerifCJKsc-Regular.otf | 2a2eae2628df83556c54018c41e20fa532c1b862c5256ae8b3f23feb918d12ca |

内置字体跟随代码部署，无需读取管理员电脑的字体。管理员上传的字体保存在 `assets/fonts/uploaded/`，不会提交到 Git，应随部署数据单独备份。
