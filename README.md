# NLP Final Project QA System

## Demo

- Streamlit Demo: https://nlpfinalproject-upb4xazc5pfxp9lsrauywe.streamlit.app/

這個專案是一個以課程講義為知識來源的問答系統，前端使用 Streamlit。文件會先被解析與切塊，建立本地檢索索引；使用者提問後，系統會依照問題語言做不同的查詢前處理，再用 Whoosh 反向索引與 Whoosh BM25F 找出最相關的 chunks，最後由 Gemini 生成答案。除了單題互動式問答之外，現在也支援匯入 `.csv` 批次問題檔，逐題作答後回傳結果 `.csv`。

## 目前版本重點

- 文件來源以英文教材為主，例如 `English_docs/`
- 使用者可以用中文或英文提問
- 支援單題問答與 `.csv` 批次問答兩種模式
- 中文問題：
  - 一次 Gemini 呼叫，同時回傳英文檢索句與關鍵字
- 英文問題：
  - 直接本地 `tokenize()` 抽關鍵字
  - 不額外呼叫 LLM 做關鍵字抽取
- 反向索引使用 `Whoosh`
- 排名使用 `Whoosh BM25F`
- 最終回答固定參考前 10 個 chunks
- 回答會跟隨使用者語言：
  - 中文問題回繁體中文
  - 英文問題回英文
- CSV 批次問答會保留原始欄位，並額外輸出 `answer` 欄位

## 系統流程

整體流程如下：

1. 讀取文件資料夾中的支援格式文件
2. 將文件內容解析成純文字
3. 將長文本切成 chunks
4. 建立兩份本地資料：
   - `data/bm25_chunks.jsonl`：保存 chunk 與 metadata
   - `data/whoosh_index/`：Whoosh 反向索引
5. 使用者輸入問題後：
   - 若是中文：
     - 由 Gemini 一次完成
       - 英文檢索句生成
       - 關鍵字抽取
   - 若是英文：
     - 直接用本地 `tokenize()` 取得關鍵字
     - 不再額外呼叫 LLM 做關鍵字抽取
   - 用關鍵字在 Whoosh 反向索引中找候選 chunks
   - 用 Whoosh 的 BM25F 在候選集合上做 top-k 排名
   - 取前 10 個 chunks 作為回答參考
   - 交給 Gemini 生成最終答案
6. 若使用者提供 `.csv` 問題檔：
   - 讀取 CSV 內容
   - 自動判斷問題欄位
   - 逐題重複上述 RAG 流程
   - 將答案寫入新的 `answer` 欄位並輸出結果 CSV

## 目前檢索與回答策略

- `parse_file()`：負責解析 PDF / DOCX / DOC / ODT / TXT
- `chunk_text()`：將文件內容切成可檢索的文字片段
- `build_whoosh_index()`：建立套件版反向索引
- `parse_questions_csv_bytes()`：解析上傳或本機路徑的 CSV 問題檔
- `preprocess_cjk_question_for_retrieval()`：中文問題一次完成英文檢索句與關鍵字前處理
- `search_whoosh_candidates()`：用關鍵字從 Whoosh 找候選 chunks
- `search_whoosh_bm25()`：用 Whoosh BM25F 對候選 chunks 做 top-k 排名
- `answer_with_gemini()`：根據問題與前 10 個 chunks 生成回答
- `build_batch_answer_csv()`：逐題回答並產生可下載的結果 CSV

補充：

- chunks 是給 LLM 的輔助參考
- 問答頁會在答案後面顯示本次使用到的 chunks
- LLM 可以結合既有知識回答，但若與文件內容衝突，應以文件為主

## 使用到的模型與套件

- `Whoosh`
  - 建立反向索引
  - 候選文件檢索
  - BM25F 排名
- `Gemini`
  - 中文問題前處理
  - 最終答案生成
- `jieba`
  - 中文分詞
- `GCP ADC`
  - 提供 Gemini 呼叫所需認證

## 支援文件格式

系統目前支援以下格式：

- `.pdf`
- `.docx`
- `.doc`
- `.odt`
- `.txt`

其中：

- `.pdf` 使用 `pypdf`
- `.docx` 先嘗試 `python-docx`，失敗時回退到 XML 解析
- `.doc` 會優先嘗試 Windows Word COM，失敗時回退到 LibreOffice
- `.odt` 先解析 XML，失敗時回退到 LibreOffice
- `.txt` 會依序嘗試多種編碼讀取

## 專案結構

```text
NLP_final_project/
├─ app.py
├─ test_parser.py
├─ README.md
├─ requirements.txt
├─ settings.json
├─ settings.example.json
├─ English_docs/
└─ data/
   ├─ bm25_chunks.jsonl
   └─ whoosh_index/
```

說明：

- `app.py`：主程式與 Streamlit UI
- `test_parser.py`：批次測試 `English_docs/` 文件是否可成功解析與切塊
- `English_docs/`：英文教材資料夾
- `data/bm25_chunks.jsonl`：chunk 與 metadata
- `data/whoosh_index/`：Whoosh 反向索引資料

## 安裝方式

1. 安裝套件

```bash
pip install -r requirements.txt
```

2. 設定 GCP ADC

```bash
gcloud auth application-default login
gcloud auth application-default set-quota-project <YOUR_GCP_PROJECT_ID>
```

3. 啟動系統

```bash
streamlit run app.py
```

## 認證方式

這個專案目前同時支援兩種 Google 認證方式：

1. 本機開發：
   - 使用 Application Default Credentials (ADC)
   - 透過以下指令設定：

```bash
gcloud auth application-default login
gcloud auth application-default set-quota-project <YOUR_GCP_PROJECT_ID>
```

2. Streamlit Community Cloud：
   - 使用 `st.secrets["gcp_service_account"]`
   - 程式啟動時會自動把 secrets 內容寫成暫存 JSON 憑證檔
   - 並自動設定 `GOOGLE_APPLICATION_CREDENTIALS`

也就是說：

- 本機跑 app 時，可以直接沿用你目前的 ADC
- 部署到 Streamlit Cloud 時，不需要本機 `gcloud login`
- 只要在 app 的 secrets 中提供 service account JSON 即可

## Streamlit Cloud 部署

如果你要把這個專案部署到 Streamlit Community Cloud，建議使用：

- GitHub repo 部署
- Streamlit secrets 管理 GCP service account

你需要在 Streamlit app 的 secrets 中加入類似內容：

```toml
[gcp_service_account]
type = "service_account"
project_id = "your-project-id"
private_key_id = "..."
private_key = "-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----\n"
client_email = "your-service-account@your-project.iam.gserviceaccount.com"
client_id = "..."
auth_uri = "https://accounts.google.com/o/oauth2/auth"
token_uri = "https://oauth2.googleapis.com/token"
auth_provider_x509_cert_url = "https://www.googleapis.com/oauth2/v1/certs"
client_x509_cert_url = "..."
universe_domain = "googleapis.com"
```

部署時注意：

- 不要把 service account JSON 直接 commit 到 GitHub
- 用 Streamlit Community Cloud 的 Secrets 功能保存憑證
- service account 只給最小必要權限

## 使用方式

1. 啟動 app 後，系統會直接使用 `settings.json` 的預設設定
2. 問答介面是 demo 預設首頁
3. 在問答頁上半部可直接進行單題提問
4. 在問答頁下半部可使用「CSV 批次問答」：
   - 上傳 `.csv` 檔案，或輸入本機 `.csv` 路徑
   - 按下「開始批次回答 CSV」
   - 完成後下載結果 `.csv`
5. 若要更新資料，進入「建立索引」頁
6. 建立索引前可先用「測試解析與切塊」檢查文件
7. 按下「重新建立索引」後，系統會重建：
   - `bm25_chunks.jsonl`
   - `whoosh_index`
8. 回到問答頁直接提問，或執行 CSV 批次問答

## CSV 批次問答格式

- 支援上傳 `.csv`，也支援直接輸入本機檔案路徑
- 若 CSV 第一列是欄位名稱，系統會優先尋找這些問題欄位名稱：
  - `question`
  - `questions`
  - `query`
  - `prompt`
  - `問題`
  - `題目`
  - `問句`
- 若 CSV 沒有欄位名稱且只有單欄，系統會把每一列視為一個問題
- 若 CSV 有多欄但沒有明確欄位名稱，系統預設使用第一欄作為問題欄位

範例一：單欄、無 header

```csv
什麼是 Time-homogeneous Markov process？
幾月幾號是期中考？
```

範例二：有 header

```csv
question
什麼是 Time-homogeneous Markov process？
幾月幾號是期中考？
```

輸出格式：

- 系統會保留原始欄位
- 另外新增一欄 `answer`
- 下載檔名格式預設為 `<原檔名>_answers.csv`

## Demo 設計

- 問答介面為預設首頁
- 左側設定欄已隱藏
- 建立索引頁需要密碼才能進入
- 建立索引頁主要是給建立者維護資料，不是一般 demo 使用者操作區

## `test_parser.py` 的用途

`test_parser.py` 主要用來驗證：

- `English_docs/` 內的文件是否都能成功解析
- 每份文件是否都能成功切成 chunks
- 分詞流程是否能正常執行

如果你想先確認教材是否可用，再進 app 建索引，可以先執行：

```bash
python test_parser.py
```

## 注意事項

- 只要更新檢索索引結構或文件內容，就應重新建立索引
- 若要使用中文問題前處理與最終 Gemini 回答，仍需要正確設定 GCP ADC
- 英文問題的關鍵字抽取走本地 `tokenize()`，不需要額外的 LLM 前處理呼叫
- `.doc` 解析在 Windows 環境會比較依賴本機 Word 或 LibreOffice
- PDF 若本身格式較亂，`pypdf` 可能會出現 warning，但不一定代表解析失敗

## 目前版本摘要

這份專案目前可以描述為：

- 一個以英文教材為知識來源的問答系統
- 支援中文與英文提問
- 中文問題用一次 Gemini 前處理完成檢索句與關鍵字
- 英文問題直接本地 tokenize，不額外呼叫 LLM 抽關鍵字
- 使用 `Whoosh inverted index + Whoosh BM25F` 完成文字檢索
- 不依賴向量資料庫與 embedding 流程
