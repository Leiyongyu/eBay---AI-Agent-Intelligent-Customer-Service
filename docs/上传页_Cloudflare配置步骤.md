# 买家上传页：Cloudflare 配置步骤

上传页地址：`https://upload.diwhx.xyz/upload/`。买家在页面上校验订单、填问题，视频等大文件由浏览器**分块直接传到 Cloudflare R2**（不经过本机，国外买家也快），
本机后台再把文件从 R2 拉回 `data/after_sales_files/店铺/订单号_时间戳_序号.扩展名`，拉回后删掉 R2 里的文件。

下面 7 步都在 Cloudflare 后台（dash.cloudflare.com）做，做完把密钥填进项目根目录 `.env`，再重启 main.py。

---

## 1. 账号开启两步验证

右上角头像 → **My Profile** → **Authentication** → **Two-Factor Authentication** → 按提示用手机验证器绑定。
（R2 和隧道都挂在这个账号下，账号被盗就能删数据、改路由。）

## 2. 开通 R2、设置费用提醒

1. 左侧菜单 **R2 Object Storage** → **Purchase R2 Plan / Get started**，绑定付款方式（有免费额度：每月 10 GB 存储、上传下载请求有免费次数；R2 **不收流量费**）。
2. **Manage Account → Billing → Notifications**（或 **Notifications → Add** → Billing），加一条用量/费用提醒，例如月费超过 5 美元就发邮件。

## 3. 建私有存储桶

1. **R2 → Create bucket**：
   - 名字：例如 `aftersales-uploads`（记下来，填 `.env` 的 `R2_BUCKET`）；
   - Location：选 **Eastern Europe (EEUR)** 或 **Western Europe (WEUR)**（买家多在德国、英国）；
   - 不要开 Public access（保持私有）。
2. 进存储桶 → **Settings → CORS Policy → Add CORS policy**，粘贴：

```json
[
  {
    "AllowedOrigins": ["https://upload.diwhx.xyz"],
    "AllowedMethods": ["PUT"],
    "AllowedHeaders": ["*"],
    "ExposeHeaders": ["ETag"],
    "MaxAgeSeconds": 3600
  }
]
```

   > `ExposeHeaders: ETag` 必须有：浏览器要读到每个分块的 ETag，最后才能合并成完整文件。

3. 同一页 **Object lifecycle rules → Add rule**，加两条：
   - 删除对象：**30 天**后删除（兜底：万一本机长期没开机，文件也不会一直留在 R2）；
   - **Abort incomplete multipart uploads**：**1 天**（买家没传完留下的残块）。

## 4. 建只能读写这个存储桶的 API 令牌

1. **R2 → Manage R2 API Tokens → Create API token**：
   - 权限：**Object Read & Write**；
   - 范围：**Apply to specific buckets only** → 选上一步的存储桶；
   - TTL：可以不设（永久），也可以设一年到期再换。
2. 创建后页面只显示一次，记下：
   - **Access Key ID** → `.env` 的 `R2_ACCESS_KEY_ID`
   - **Secret Access Key** → `.env` 的 `R2_SECRET_ACCESS_KEY`
   - 页面上的 **Account ID**（或 R2 概览页右侧）→ `.env` 的 `R2_ACCOUNT_ID`

## 5. 建 Turnstile 人机验证

1. 左侧 **Turnstile → Add widget**：
   - Widget name：`upload page`；
   - Hostname：`upload.diwhx.xyz`；
   - Widget mode：**Managed**。
2. 记下：
   - **Site Key** → `.env` 的 `TURNSTILE_SITE_KEY`
   - **Secret Key** → `.env` 的 `TURNSTILE_SECRET_KEY`

## 6. 给隧道加公网主机名（只放行 /upload）

1. **Zero Trust → Networks → Tunnels** → 现有的隧道 `ebay-cs-webhook` → **Configure → Public Hostname → Add a public hostname**：
   - Subdomain：`upload`，Domain：`diwhx.xyz`；
   - Path：`^/upload(/.*)?$`（只放行 /upload 开头的路径，工作台、接口都不会暴露）；
   - Service：`HTTP` → `localhost:8000`。
2. 保存后，用手机流量打开 `https://upload.diwhx.xyz/upload/`：能看到上传页（或“暂未开放”提示）就对了；
   打开 `https://upload.diwhx.xyz/` 应该是 404（说明工作台没有暴露出去）。

## 7. 给订单校验加一条限流规则

**Websites → diwhx.xyz → Security → WAF → Rate limiting rules → Create rule**：
- 条件：Hostname 等于 `upload.diwhx.xyz` **且** URI Path 等于 `/upload/api/verify`；
- 同一 IP 每 1 分钟超过 **10** 次 → **Block 10 分钟**。

（系统里还有自己的限流：同一 IP 每小时 20 次、同一订单每小时 10 次；Cloudflare 这条挡在最外面。）

---

## 填 `.env` 并启用

```
UPLOAD_PAGE_ENABLED=true
UPLOAD_PUBLIC_URL=https://upload.diwhx.xyz
R2_ACCOUNT_ID=（第 4 步的 Account ID）
R2_ACCESS_KEY_ID=（第 4 步）
R2_SECRET_ACCESS_KEY=（第 4 步）
R2_BUCKET=（第 3 步的存储桶名）
TURNSTILE_SITE_KEY=（第 5 步）
TURNSTILE_SECRET_KEY=（第 5 步）
```

其余（每个订单 5 GB、最多 20 个文件、链接 7 天、分块 16 MB）用默认值即可。填好后在 PyCharm 里 Ctrl+Alt+Y，重启 main.py。
工作台打开任意会话，“售后附件”一栏的 **生成上传链接** 按钮变成可点，就说明上传页已开通。

## 上线前实测

1. 在会话里点“生成上传链接”，选订单，复制链接；
2. 用手机（最好开欧洲的网络/VPN）打开链接，选问题类型，上传一个 500 MB 左右的视频，中途关掉网络再打开，看能否续传；
3. 提交后几分钟内，工作台这个会话应出现“售后附件 · 上传页”，会话被标成“待售后”，备注里是买家填的问题；
4. R2 存储桶里对应的文件应已被删掉。
