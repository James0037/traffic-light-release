# GitHub Pages 部署 Traffic Light 官网发布端

推荐优先使用 GitHub Pages。原因：静态站无需服务器，免费 HTTPS，工作流已经生成，当前安装包小于 GitHub 单文件 100MB 限制，后续只需要推送 `release_site` 即可重新部署。

## 你需要本人完成

1. 注册或登录 GitHub。
2. 创建一个仓库，例如 `traffic-light-release`。
3. 把当前项目推送到该仓库。
4. 在仓库 Settings 中开启 Pages，并允许 GitHub Actions 部署。
5. 如果需要自定义域名，在域名服务商处配置 DNS。

## 已经准备好的文件

```text
.github/workflows/deploy-release-site.yml
release_site/index.html
release_site/releases/latest/update.json
release_site/releases/v2.1/update.json
release_site/downloads/Traffic_Light_v2.1.0.zip
release_site/checksums/SHA256SUMS.txt
release_site/CNAME.example
```

## 推荐仓库内容

建议创建一个专门的官网发布仓库，只放下面两项：

```text
release_site/
.github/workflows/deploy-release-site.yml
```

不要把 `release/` 里的完整发布包、源码包或其它大 zip 提交到 GitHub。GitHub 普通仓库单文件限制为 100 MB，而完整发布包会超过该限制。官网真正需要公开下载的文件已经在 `release_site/downloads/` 中，当前 V2.1 安装包约 69 MB，可以用于 GitHub Pages。

## 推送到 GitHub

在项目根目录执行：

```bat
git init
git add release_site .github/workflows/deploy-release-site.yml
git commit -m "Release Traffic Light V2.1 website"
git branch -M main
git remote add origin https://github.com/你的用户名/traffic-light-release.git
git push -u origin main
```

如果仓库已经存在，只需要：

```bat
git add release_site .github/workflows/deploy-release-site.yml
git commit -m "Update release site"
git push
```

## 开启 Pages

1. 打开 GitHub 仓库。
2. 进入 `Settings > Pages`。
3. 在 `Build and deployment` 中把 Source 选择为 `GitHub Actions`。
4. 推送 `main` 分支，或进入 `Actions` 页面手动运行 `Deploy Traffic Light release site`。
5. 工作流成功后，页面会显示公网地址，例如：

```text
https://你的用户名.github.io/traffic-light-release/
```

如果项目站点带仓库名路径，客户端更新地址是：

```text
https://你的用户名.github.io/traffic-light-release/releases/latest/update.json
```

如果绑定自定义域名，例如 `https://download.example.com/`，客户端更新地址是：

```text
https://download.example.com/releases/latest/update.json
```

## 自定义域名

1. 复制 `release_site/CNAME.example` 为 `release_site/CNAME`。
2. 把内容改成你的域名，例如：

```text
download.example.com
```

3. 在域名服务商 DNS 中添加 CNAME：

```text
download.example.com -> 你的用户名.github.io
```

4. 在 GitHub Pages 的 Custom domain 中填写同一域名。
5. 等待 HTTPS 证书生效。

## 部署后验证

把下面地址替换为你的公网地址：

```bat
python release_site\scripts\verify_public_update.py https://你的公网地址/ --download
```

验证项包括：首页、update.json、下载包、文件大小、SHA256。

## 客户端联调

1. 打开 Traffic Light。
2. 进入 `参数设置 > 运行维护 > 软件更新`。
3. 填写公网地址：

```text
https://你的公网地址/releases/latest/update.json
```

4. 点击“检查更新”。
5. 如果远程版本高于本机版本，客户端会显示更新说明、下载升级包并校验 SHA256。

## 回滚

如果新版本发布错误，把 `release_site/releases/latest/update.json` 替换为上一版内容，重新推送即可。固定版本目录如 `releases/v2.1/update.json` 不删除，便于审计和回滚。
