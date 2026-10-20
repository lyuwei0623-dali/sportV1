# V3 三項運動純機率版部署清單

這是一個完整的新專案。請將**本資料夾所有檔案與資料夾**覆蓋至 GitHub repository 根目錄；不要把舊版的同名程式檔混用。

必須上傳：

- `.streamlit/config.toml`
- `.streamlit/secrets.example.toml`（範例，不含真正密碼）
- `app.py`
- `forecasting.py`
- `models.py`
- `providers.py`
- `service.py`
- `storage.py`
- `ui.py`
- `database_setup.sql`
- `requirements.txt`
- `README.md`
- `tests/`
- `logo.png`

必須在 Streamlit Secrets 填入的值，請看 `README.md` 的完整範例。真正的 Secrets 不可上傳 GitHub。

Supabase 只需要在 SQL Editor 執行一次 `database_setup.sql`。之後由管理員建立並保存 MLB、NBA 或歐洲足球快照，會員頁面只讀取已保存內容。
