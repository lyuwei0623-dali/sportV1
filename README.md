# 維大力體育｜Audit V3.1

繁體中文 Streamlit 賽事與模型觀察 APP，涵蓋 MLB、NBA、英超、西甲、德甲、法甲、義甲、歐冠。

**現階段不是已驗證的下注推薦引擎。** 模型均顯示 PASS，提供估計勝率、實際線的過盤分布與估計EV。尚未完成樣本外校準，不宣稱與任何權威機構使用同一套算法，更不保證獲利。

## 這次修正

- 選定賽事日涵蓋台灣當日12:00～隔日12:00前：例如10/25包含當晚＋10/26凌晨／上午，不含10/25上午與10/26晚上。會員、保存、人工校正及回查採同一範圍。
- 舊混日快照再次篩選，範圍外紀錄封存不刪；舊總計暫藏直到回查。舊快照需管理員重建才能補齊隔日賽程，人工發布版需重新發布。
- 人工校正隊名共用繁中顯示表，場次附台灣開賽時間；來源隊名及ID不改動。已結束賽事仍可查詢，但不能建立假的歷史預測。
- NBA ESPN 賽季使用結束年份；樣本不足使用上一季會清楚揭露。
- 盤口以主客隊與開賽時間嚴格配對，同莊家不混線，拒絕過期／非有限值／不完整雙邊盤。
- 保存 `h2h,spreads,totals` 的十進位賠率，按全贏／半贏／走盤／半輸／全輸計算估計EV。增加h2h會多耗一項市場的API額度，60秒記憶體快取降低重複請求。
- 修正客隊讓分方向、足球獨贏和局輸盤、90分鐘結算與NBA整數線走盤機率。
- 開賽後鎖定舊預測，首筆＋最近29次修訂留存；空賽程／失敗不刪舊資料。
- 模型參考線、舊版與缺乏賽前時間證據的紀錄不列入新版績效。
- 健康摘要、匿名追蹤碼、GitHub錯誤回報、離線測試、公開來源排程檢查。

## 資料來源與模型限制

MLB使用 MLB Stats API 的團隊攻守與預定先發；NBA使用 ESPN 團隊得失分；足球以API-Football為主，失敗或空資料改用ESPN。The Odds API提供盤口與賠率，**賠率不會用來偷偷改高模型勝率**。

缺資料即標示不足，不用固定均分底板補成可信機率。足球未評估球員影響權重，不以傷停人數固定扣分。NBA轉隊／傷停／輪換、MLB打線／牛棚負荷／天氣未完整建模。足球獨立卜瓦松、NBA常態變異與MLB延長賽近似均未校準。

會員唯讀已保存快照，**不是背景即時盤口**。只有管理員按下按鈕才會建立推薦快照；健康檢查不會代替管理員發布。

## 現有部署更新

此repository的 `main` 為部署來源，入口 `app.py`；沿用現有Supabase表，不需要重建或刪除資料。舊快照保留但明示舊版。重新回查可修正舊版結算，不能把舊結果冒充新版績效。

密碼與金鑰只能放在Streamlit **Settings → Secrets**。管理員密碼與會員密碼由使用者自行儲存，不在程式碼提供預設：

```toml
APP_ADMIN_PASSWORD = "由管理員設定的密碼"
APP_MEMBER_PASSWORD = "由管理員設定的會員密碼"
SUPABASE_URL = "https://你的專案.supabase.co"
SUPABASE_SECRET_KEY = "從平台取得的伺服器端金鑰"
THE_ODDS_API_KEY = "從來源取得的金鑰"
API_FOOTBALL_KEY = "從來源取得的金鑰"
REQUIRE_SUPABASE = "1"
```

兼容舊的 `ODDS_API_KEY` 與 `SUPABASE_SERVICE_ROLE_KEY` 名稱。不把金鑰貼進GitHub、Issue或日誌。若API-Football未恢復，可繼續使用ESPN備援，缺的進階統計不假裝已取得。

## 驗證與追蹤

```sh
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python health_check.py
```

具體維護、校準驗收與回復程序見 [MAINTENANCE.md](MAINTENANCE.md)。

來源契約參考：[The Odds API V4](https://the-odds-api.com/liveapi/guides/v4/)、[Streamlit Secrets](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/secrets-management)。ESPN公開端點非穩定SLA；日常健康檢查不能代替完整雙來源賽程驗證。
