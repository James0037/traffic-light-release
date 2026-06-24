# Netlify 部署说明

Netlify 可直接发布静态目录，并支持 `_headers`、`_redirects` 和 `netlify.toml`。安装包较大时，请先用验证脚本确认下载和 SHA256。

## 操作步骤

1. 登录 Netlify。
2. 选择 `Add new site > Import an existing project`。
3. 连接 GitHub 仓库。
4. 构建设置填写：

```text
Base directory: 留空
Build command: 留空
Publish directory: release_site
```

5. 点击 Deploy。

## 验证

```bat
python release_site\scripts\verify_public_update.py https://你的站点.netlify.app/ --download
```

客户端更新地址：

```text
https://你的站点.netlify.app/releases/latest/update.json
```

## 自定义域名

在 Netlify 的 Domain management 中添加域名，并按提示配置 DNS。HTTPS 通常由 Netlify 自动签发。
