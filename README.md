# seo-infra

SEO 报表基础设施仓库，集中管理所有项目通用的 SEO 周报自动化工作流与脚本。

## 目录结构

```
.github/workflows/
  seo-report-core.yml    # 可复用工作流（Reusable Workflow）核心逻辑
scripts/
  seo_weekly_summary.py      # Telegram 极简摘要生成
  generate_weekly_markdown.py # 语义聚类深度周报生成（支持 CTR 异常词分析 + 趋势 Section）
  trend_scout.py             # 多源趋势监控（Google Trends/HN/Reddit，全免费）
  patch_report_escaping.py   # claude-seo 热修复补丁
```

## 如何在新项目中接入

在你的项目仓库创建 `.github/workflows/seo-weekly-report.yml`，内容如下：

```yaml
name: "SEO Weekly Report"

on:
  schedule:
    - cron: '16 2 * * 5'  # 每周五北京时间 10:16，各项目错开时间
  workflow_dispatch:

jobs:
  seo-report:
    uses: zw0306/seo-infra/.github/workflows/seo-report-core.yml@main
    with:
      seo_domain: ${{ vars.SEO_DOMAIN }}
      ga4_property_id: ${{ vars.GA4_PROPERTY_ID }}
      seo_niche_keywords: ${{ vars.SEO_NICHE_KEYWORDS }}  # 可选，不配置则自动从 domain 推断
    secrets:
      GOOGLE_API_KEY: ${{ secrets.GOOGLE_API_KEY }}
      GOOGLE_SERVICE_ACCOUNT_JSON: ${{ secrets.GOOGLE_SERVICE_ACCOUNT_JSON }}
      TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
      TELEGRAM_TARGET_CHAT: ${{ secrets.TELEGRAM_TARGET_CHAT }}
```

## 每个项目需要配置的变量

在项目 GitHub 仓库的 **Settings → Secrets and variables → Actions** 中配置：

### Secrets（机密）
| 变量名 | 说明 |
|---|---|
| `GOOGLE_API_KEY` | Google Cloud API Key |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Service Account 完整 JSON 内容 |
| `TELEGRAM_BOT_TOKEN` | Telegram 机器人 Token |
| `TELEGRAM_TARGET_CHAT` | Telegram Chat ID |

### Variables（公开变量）
| 变量名 | 示例 | 说明 |
|---|---|---|
| `SEO_DOMAIN` | `piano-sheets.org` | 不含 https:// 的域名 |
| `GA4_PROPERTY_ID` | `412345678` | GA4 纯数字 Property ID |
| `SEO_NICHE_KEYWORDS` | `piano sheet music,free piano sheets,sheet music pdf` | 趋势监控关键词，逗号分隔。**可选**，不配置则自动从 `SEO_DOMAIN` 推断 |


