# Vercel 部署说明

Vercel 可部署静态官网，但它更偏前端应用托管。安装包下载体积较大时，需要确认当前账号的静态文件限制和带宽策略。

## 操作步骤

1. 登录 Vercel。
2. 导入 GitHub 仓库。
3. Project Settings 中填写：

```text
Framework Preset: Other
Build Command: 留空
Output Directory: release_site
Install Command: 留空
```

4. 确认 `release_site/vercel.json` 已存在。
5. 点击 Deploy。

## 验证

```bat
python release_site\scripts\verify_public_update.py https://你的项目.vercel.app/ --download
```

客户端更新地址：

```text
https://你的项目.vercel.app/releases/latest/update.json
```

## 回滚

在 Vercel Deployments 中选择上一条成功部署并 Promote，或把 `releases/latest/update.json` 改回旧版本后重新部署。
