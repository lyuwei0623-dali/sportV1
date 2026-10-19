# 維大力體育APP Restart V2｜純機率版

這是全新、獨立的程式碼，**不能與舊 V12～V23 混用**。它只做會員真正需要看的三件事：

- 哪一支球隊的**勝率較高**，以及百分比。
- 在保存當時的讓分線，哪一邊的**過盤機率較高**，以及百分比。
- 在保存當時的大小線，大分或小分的**過盤機率較高**，以及百分比。

本版只呈現資料模型計算出的機率。The Odds API 只提供讓分／大小的**線**；所有百分比由球隊、先發／球員與傷停資料的模型產生。

## 會員端會看到什麼

每場固定有三格：

| 格子 | 會員看到的內容 |
|---|---|
| 勝率最高 | 主隊或客隊勝，以及勝率。例如：`主隊勝｜58.4%`。足球的和局機率仍另列，不能被隱藏。 |
| 讓分過盤率 | 實際保存的讓分線下，較可能過盤的一方與機率。例如：`客隊 +1.5｜61.2%`。 |
| 大小過盤率 | 實際保存的大小線下，大／小分較可能過盤的一方與機率。例如：`大分 8.5｜56.8%`。 |

若資料來源暫時沒有實際讓分或大小線，APP **仍會給完整三格結果**，但會以紫色的「模型基準線」清楚標示為**非實際可下注盤**。這避免會員看到空白或「無法評估」，也不會把模型基準線偽裝成市場盤口。

## 真正使用的資料

### MLB

- MLB Stats API：賽程、主客隊、賽季戰績、球隊每場得分、OPS、團隊 ERA／WHIP、已公布的預定先發、先發 ERA／WHIP／局數。
- 模型：對手打擊與投球能力交叉計算，再以已公布先發做保守調整；打線、牛棚使用與天氣尚未確認時會顯示風險，不會假裝已納入。
- The Odds API：只讀取讓分與大小線，不讀取、不儲存、不顯示價格。

### 歐洲足球（英超、西甲、義甲、德甲、法甲、歐冠）

- API-Football：賽程、聯賽攻守、主客場進失球、近期狀態、零封／未進球率、可取得的傷停／停賽名單。
- 模型：先由主客場攻守建立預估進球，再以近期狀態、零封／未進球率與傷停人數做小幅、可說明的調整；絕不套用所有隊伍相同的 `1.40 / 1.10` 底板。
- The Odds API：只讀取讓分與大小線。

足球的「主預測比分」是**勝率較高一隊的比分情境中最可能的單一比分**，並同步顯示它在全比分分布中的命中機率。它不是保證，也不會因為 1:1 是常見比分就固定顯示 1:1。

## 部署方式

### 1. 建立全新的 GitHub repository

建立空白 repository，例如 `dali-sports-restart`。把這個資料夾**內的所有檔案**放到 repository 根目錄後 Commit。

不要將任何舊 APP、舊 SQLite、舊 `app.py`、舊 `requirements.txt` 或舊更新檔搬進來。

### 2. 建立 Supabase 永久資料庫

在 Supabase 建立新的 project，進入 **SQL Editor**，完整貼上並執行 `database_setup.sql` 一次。

這個架構只使用一張正常的快照表：自動建立的快照會保存，會員只讀取保存版本；重新部署也不會遺失。

### 3. 建立 Streamlit Cloud App

1. 建立新的 Streamlit App，選剛建立的 GitHub repository。
2. **Main file path** 設為 `app.py`。
3. 到 **Settings → Secrets**，貼上以下內容並填入自己的值。
4. 儲存後等待重新部署。

```toml
APP_ADMIN_PASSWORD = "一組只有你知道的長密碼"
APP_MEMBER_PASSWORD = "會員登入密碼"
SUPABASE_URL = "https://你的專案.supabase.co"
SUPABASE_SERVICE_ROLE_KEY = "Supabase 的 service_role key"
API_FOOTBALL_KEY = "你的 API-Football 金鑰"
API_FOOTBALL_INCLUDE_INJURIES = "1"
THE_ODDS_API_KEY = "你的 The Odds API 金鑰"
ODDS_REGIONS = "eu"
REQUIRE_SUPABASE = "1"
```

`SUPABASE_SERVICE_ROLE_KEY`、各 API Key、管理員密碼與會員密碼都只能放在 Streamlit Secrets；不得放進 GitHub。

## 每日操作

1. 用管理員密碼登入。
2. 選擇 MLB 或歐洲足球與比賽日期。
3. 按「建立並保存自動快照」。
4. 建立成功後，會員立即可以查看這份自動快照；管理員想確認時再按「標記為管理員確認版」。

手動確認是發布權限，不是模型能否產生結果的前提。只要球隊資料成功抓到，會員就會有勝率、讓分過盤率與大小過盤率；沒有實際線時會明確標記模型基準線。

## 真實限制

- 機率不是保證結果，正確比分尤其通常只會有較低的單一命中機率。
- API 尚未提供或尚未公布的先發／傷停，不能被假裝已知。
- 若球隊資料本身不足，APP 會拒絕產生虛構百分比並保留舊快照；這與缺少盤口線不同。
- 新抓取失敗不會用空資料覆蓋舊快照。

## 本機測試

```bash
python -m pip install -r requirements.txt
python -m unittest discover -s tests -q
streamlit run app.py
```

本機若未設定 Supabase，可暫時設 `REQUIRE_SUPABASE = "0"` 使用本機 SQLite 測畫面；這個模式不能用於正式會員部署。
