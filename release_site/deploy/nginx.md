# 普通服务器 + Nginx 部署说明

Nginx 适合公司内网、云服务器、对象存储反向代理或高下载量场景。优点是文件大小和流量更可控，缺点是需要你本人购买服务器、配置域名和证书。

## 上传目录

把 `release_site` 目录内的全部文件上传到服务器，例如：

```text
/var/www/traffic-light/
```

## Nginx 配置示例

```nginx
server {
    listen 80;
    server_name download.example.com;
    root /var/www/traffic-light;
    index index.html;

    location = /releases/latest/update.json {
        add_header Cache-Control "no-cache, no-store, must-revalidate";
        default_type application/json;
        try_files $uri =404;
    }

    location /releases/ {
        default_type application/json;
        try_files $uri =404;
    }

    location /downloads/ {
        add_header Content-Disposition "attachment";
        try_files $uri =404;
    }

    location / {
        try_files $uri $uri/ /404.html;
    }
}
```

生产环境建议使用 HTTPS。可以用云厂商证书或 Let's Encrypt。

## 验证

```bat
python release_site\scripts\verify_public_update.py https://download.example.com/ --download
```

客户端更新地址：

```text
https://download.example.com/releases/latest/update.json
```
