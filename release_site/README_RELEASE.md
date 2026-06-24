# Traffic Light 官网与自动更新发布端

本目录是 Traffic Light（棱镜视流）的公网可部署官网发布端。它只包含静态文件，可以部署到 GitHub Pages、Cloudflare Pages、Vercel、Netlify、Nginx 或对象存储静态网站。客户端通过 `releases/latest/update.json` 检测新版本，通过 `download_url` 下载升级包，并用 `sha256` 做完整性校验。

## 推荐方案

我建议优先使用 **GitHub Pages**：

1. 不需要服务器和数据库；
2. 免费 HTTPS；
3. 已经生成 GitHub Actions 自动部署工作流；
4. 当前安装包约 69 MB，低于 GitHub 单文件 100 MB 限制；
5. 对不熟悉网站部署的使用者，操作成本最低。

如果后续安装包超过 100 MB、下载量很大或需要企业内网发布，改用 Nginx、对象存储或专门下载服务器。

GitHub Pages 仓库建议只提交 `release_site/` 和 `.github/workflows/deploy-release-site.yml`。不要把 `release/` 目录下的完整发布包、源码包或大 zip 提交到 GitHub 仓库，因为 GitHub 普通仓库单文件限制为 100 MB。

## 目录结构

```text
release_site/
  .nojekyll
  index.html
  404.html
  update.json
  _headers
  _redirects
  CNAME.example
  netlify.toml
  vercel.json
  wrangler.toml
  robots.txt
  sitemap.xml
  assets/
    style.css
    logo.png
    screenshots/
  releases/
    latest/update.json
    v2.1/update.json
  downloads/
    Traffic_Light_v2.1.0.zip
  checksums/
    SHA256SUMS.txt
    Traffic_Light_v2.1.0.zip.sha256
    update.json.sha256
  scripts/
    generate_sha256.py
    prepare_release.py
    validate_release.py
    verify_public_update.py
  deploy/
    github-pages.md
    cloudflare-pages.md
    vercel.md
    netlify.md
    nginx.md
```

## 核心地址

本地测试地址：

```text
http://127.0.0.1:8000/releases/latest/update.json
```

公网正式地址格式：

```text
https://你的公网域名/releases/latest/update.json
```

`latest/update.json` 永远指向最新稳定版，`releases/v2.1/update.json` 固定保留 V2.1 信息。回滚时只需要把 `latest/update.json` 改回旧版本内容。

当前 `downloads/Traffic_Light_v2.1.0.zip` 由现有正式打包产物 `release/Traffic Light_V2.1_标准安装包.zip` 复制生成。若临时使用测试 zip 验证流程，正式发布前必须替换为真实安装包或真实压缩包，并重新生成 SHA256、文件大小和两个 `update.json`。

## 本地测试

进入 `release_site`：

```bat
cd /d D:\pythonProject\迭代版本\Traffic Light_V2.1.0\release_site
python -m http.server 8000
```

浏览器检查：

```text
http://127.0.0.1:8000/
http://127.0.0.1:8000/releases/latest/update.json
```

自动验证：

```bat
cd /d D:\pythonProject\迭代版本\Traffic Light_V2.1.0\release_site
python scripts\validate_release.py
python scripts\verify_public_update.py http://127.0.0.1:8000/ --download
```

客户端本地联调：

1. 打开 Traffic Light；
2. 进入 `参数设置 > 运行维护 > 软件更新`；
3. 更新地址填入 `http://127.0.0.1:8000/releases/latest/update.json`；
4. 点击“检查更新”。

## 公网部署

主方案文档：

```text
release_site/deploy/github-pages.md
```

备选方案：

```text
release_site/deploy/cloudflare-pages.md
release_site/deploy/vercel.md
release_site/deploy/netlify.md
release_site/deploy/nginx.md
```

GitHub Pages 工作流已经放在：

```text
.github/workflows/deploy-release-site.yml
```

该工作流采用 GitHub 官方推荐的 Pages Actions 发布方式：`actions/configure-pages` 初始化 Pages 环境，`actions/upload-pages-artifact` 上传 `release_site` 静态目录，`actions/deploy-pages` 发布到 GitHub Pages。工作流只在 `main` 分支更新时触发，也可以在 Actions 页面手动运行。

开启 GitHub Pages：

1. 打开 GitHub 仓库；
2. 进入 `Settings > Pages`；
3. 在 `Build and deployment` 中把 `Source` 选择为 `GitHub Actions`；
4. 确认仓库 `Actions` 权限允许运行工作流；
5. 推送 `main` 分支，或进入 `Actions` 手动运行 `Deploy Traffic Light release site`；
6. 等待工作流完成后，打开部署结果中的 Pages 地址。

工作流发布目录固定为：

```text
release_site
```

发布后必须能访问下面三个关键地址：

```text
https://你的用户名.github.io/你的仓库名/
https://你的用户名.github.io/你的仓库名/releases/latest/update.json
https://你的用户名.github.io/你的仓库名/downloads/Traffic_Light_v2.1.0.zip
```

如果绑定了独立域名，例如 `https://traffic-light.cn/`，则对应地址为：

```text
https://traffic-light.cn/
https://traffic-light.cn/releases/latest/update.json
https://traffic-light.cn/downloads/Traffic_Light_v2.1.0.zip
```

该工作流没有写入任何密钥、Token、密码。GitHub Pages 发布所需权限由仓库的 Actions 运行环境提供，YAML 中只声明 `contents: read`、`pages: write`、`id-token: write`。

部署成功后运行：

```bat
python release_site\scripts\verify_public_update.py https://你的公网域名/ --download
```

验证通过后，在客户端“软件更新”里填写：

```text
https://你的公网域名/releases/latest/update.json
```

也可以在启动前通过环境变量临时切换：

```bat
set TRAFFIC_LIGHT_UPDATE_URL=https://你的公网域名/releases/latest/update.json
Traffic Light_V2.1.exe
```

## 发布 V2.2 / V2.3

1. 生成新的安装包或压缩包，例如：

```text
dist\Traffic_Light_v2.2.0.zip
```

2. 进入 `release_site` 目录，运行发布脚本：

```bat
cd /d D:\pythonProject\迭代版本\Traffic Light_V2.1.0\release_site
python scripts\prepare_release.py ^
  --version 2.2.0 ^
  --package "..\dist\Traffic_Light_v2.2.0.zip" ^
  --base-url "https://traffic-light.cn" ^
  --note "新增功能说明第一条" ^
  --note "修复问题说明第二条"
```

如果路径中有中文或空格，必须使用英文双引号：

```bat
python scripts\prepare_release.py --version 2.2.0 --package "..\dist\Traffic Light V2.2 正式包.zip" --base-url "https://traffic-light.cn"
```

脚本会自动完成：

1. 检查安装包是否存在，不存在会直接报错；
2. 复制安装包到 `downloads/`；
3. 自动生成下载文件名，例如 `Traffic_Light_v2.2.0.zip`；
4. 计算 SHA256，失败会直接报错；
5. 计算 `file_size`；
6. 写入 `releases/v2.2/update.json`，写入失败会直接报错；
7. 更新 `releases/latest/update.json`；
8. 更新根目录 `update.json`；
9. 更新 `index.html` 中的版本号、下载链接、发布时间；
10. 更新 `checksums/` 校验文件。

3. 发布前验证：

```bat
python scripts\validate_release.py
```

输出会逐项显示 `PASS` 或 `FAIL`。脚本会检查：

1. `index.html` 是否存在；
2. `releases/latest/update.json` 是否存在；
3. `latest/update.json` 是否为合法 JSON；
4. `download_url` 是否可读取；
5. `downloads/` 中是否存在对应下载包；
6. 下载包 SHA256 是否与 update.json 一致；
7. 下载包 `file_size` 是否与 update.json 一致；
8. `index.html` 的下载链接是否指向同一个文件。

如果需要机器可读报告，可以使用：

```bat
python scripts\validate_release.py --json
```

4. 推送到 GitHub 或上传到服务器。

5. 公网验证：

```bat
python scripts\verify_public_update.py https://你的公网域名/ --download
```

## SHA256 单独生成

在 `release_site` 目录内执行：

```bat
python scripts\generate_sha256.py downloads\Traffic_Light_v2.1.0.zip
```

输出示例：

```text
File: downloads\Traffic_Light_v2.1.0.zip
Size: 69314183 bytes (66.10 MB)
SHA256: 0f383ffb19828ebc5dca60cf2a394ecedc57ffcff2781bb1b9e7888e432b7bee
```

脚本支持中文路径和空格路径。如果文件路径包含空格，请用英文双引号包起来：

```bat
python scripts\generate_sha256.py "downloads\Traffic Light V2.1 测试包.zip"
```

## update.json 说明

当前格式兼容客户端解析：

```json
{
  "app_name": "Traffic Light",
  "latest_version": "2.1.0",
  "version_code": 210,
  "release_date": "2026-06-24",
  "channel": "stable",
  "minimum_supported_version": "2.0.0",
  "force_update": false,
  "package_type": "zip",
  "download_url": "https://你的公网域名/downloads/Traffic_Light_v2.1.0.zip",
  "sha256": "真实 SHA256",
  "file_size": 69314183,
  "release_notes": ["更新说明"],
  "homepage_url": "https://你的公网域名/",
  "manual_download_url": "https://你的公网域名/downloads/"
}
```

`prepare_release.py` 会默认根据 `--base-url` 写入绝对 `download_url`，例如 `https://traffic-light.cn/downloads/Traffic_Light_v2.2.0.zip`。本地测试时可以把 `--base-url` 写成 `http://127.0.0.1:8000/`。

## 回滚旧版本

1. 找到旧版本固定目录，例如 `release_site/releases/v2.1/update.json`。
2. 复制其内容覆盖 `release_site/releases/latest/update.json` 和 `release_site/update.json`。
3. 确认旧安装包仍在 `release_site/downloads/`。
4. 运行：

```bat
cd /d D:\pythonProject\迭代版本\Traffic Light_V2.1.0\release_site
python scripts\validate_release.py
```

5. 重新部署。

## 仍需用户本人完成的事项

1. 注册或登录 GitHub、Cloudflare、Vercel、Netlify 或服务器平台账号。
2. 创建 GitHub 仓库，并授权 Pages 或第三方平台读取仓库。
3. 如果需要自定义域名，购买域名。
4. 在域名服务商处配置 DNS。
5. 在部署平台绑定自定义域名并等待 HTTPS 生效。
6. 获取最终公网地址。
7. 用最终公网地址运行 `prepare_release.py --base-url "https://你的公网域名/"`。
8. 部署后用 `verify_public_update.py` 做公网下载和 SHA256 验证。
9. 在客户端软件更新设置中填入正式公网 `update.json` 地址并保存。

除上述账号、授权、域名和确认发布动作外，官网文件、部署配置、发布脚本、校验脚本和客户端切换能力已经准备好。
