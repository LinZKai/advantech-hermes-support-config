# Hermes Agent 測試劇本

一組連續的單一 session 對話，用來驗證 SOUL.md 與 foundry-iq skill 的行為。

**使用方式**：在同一個 session 中依序輸入，中途不要重開。每一輪對照「預期」與「失敗徵兆」評分。

**注意**：預期內容是根據以下六份文件推導的。知識庫中還有數百份其他文件，檢索可能合理地帶回額外資訊，這不算失敗；失敗指的是與這六份文件牴觸、或把不同文件的內容混在一起。

## 判讀前必讀：不要只比對文字層

本檔的判定標準曾經有數項是錯的，原因值得記下來。

知識庫的每份 PDF 並非只存文字。ingestion 會對圖片跑視覺模型，把 Fig.1～Fig.8 的內容轉成文字描述，與文字層一起切成 chunk 存入。一次檢索可能回傳 20～25 個 chunk，其中相當比例是圖片描述。

早期評分時只用 `pdftotext` 抽文字層去比對，因此把好幾個**確實存在於知識庫**的值判成捏造：

| 曾被誤判為捏造 | 實際位置 |
|---|---|
| `opc.tcp://10.0.0.1:4840` | D1 的 endpoint 選擇畫面描述 |
| `None - None (uatcp-uasc-ua)` | 同上 |
| `Digital_Input`／`Digital_Output`／`DI_00_DIValue`／`DI_01_DIValue` | D1 的 Address Space 畫面描述 |
| WebAccess 的 Security Policy `Basic128Rsa15`、Security Mode `Sign`、Authentication | D3 的 Fig.8／Fig.9 描述 |

**判定任何內容是否為捏造之前，一律先查檢索紀錄**（`logs/foundry_iq_retrievals.jsonl`），不要只憑 PDF 的文字層。相關指令見本檔末。

同時要記得：圖片描述中的值是二手的——由模型讀圖產生，且記錄的是某位工程師的測試環境。`10.0.0.1` 就是這樣一個值。它存在於知識庫，但不是任何人的預設值。

| 代號 | 文件 |
|---|---|
| D1 | IAG_FAQ_ADAM-6300_How to acquire IO data via UAexpert |
| D2 | IAG_FAQ_ADAM-6300_How to acquire IO data via Ignition |
| D3 | IAG_FAQ_ADAM-6300_How to acquire IO data via WebAccess |
| D4 | IAG_FAQ_ADAM-6300_How to retain module settings after power cycle |
| D5 | FAQ_How to properly distribute high-current loads across BB-USH207-B / ULI-417H USB ports |
| D6 | FAQ_Linux Driver Installation for BB or ULI Series USB Converters |

D1 與 D2 的步驟幾乎逐字相同，是本劇本主要的污染陷阱。D3 是唯一寫出帳密與 port 的文件。

---

## T1 — 基準檢索

> ADAM-6350 要怎麼用 UAexpert 讀取 IO 資料？

**預期分類**：`[NEW_TOPIC]`，以原問題檢索。

**應該出現**：D1 的五個步驟——在 UAexpert 的 Custom Discovery 新增 OPC UA server、選擇連線類型（anonymous/None 或 security/Basic128Rsa15-Sign）並填帳密、連線後信任 ADAM-6350 送來的憑證、到 Adam/Apax .NET Utility 的 Certificates 頁籤信任被拒絕的 UAexpert 憑證、最後展開樹狀結構把 tag 拖曳到 Data Access View 監看。

**失敗徵兆**：
- 出現 Ignition 或 WebAccess/SCADA 的**流程步驟**。D1 D2 高度相似，若檢索同時帶回兩份而 agent 沒有區分，會混講。
- 出現 `ObjectSFolder` 開頭的完整 node path（`1:1:/ObjectSFolder/...`）。這個格式是 WebAccess 的 tag 位址寫法，只在 D3，不屬於 UAexpert 流程。
- 把 `10.0.0.1` 當成該填的值，而非明確說明那是文件截圖中的示範環境。

**以下不是失敗徵兆**（曾誤列，見前述說明）：
- `4840`、`opc.tcp://`、`Digital_Input`、`DI_00_DIValue`、`10.0.0.1` 本身。這些都在 D1 的圖片描述中，引用它們是有依據的。

---

## T2 — 縮寫追問（連線類型）

> 連線類型要選哪個？

**預期分類**：`[FOLLOW_UP]`，改寫後才查詢。改寫應接近「UAexpert 連線 ADAM-6350 時的 OPC UA 安全模式選項」。

**應該出現**：兩個選項——anonymous（None）或 security（Basic128Rsa15-Sign）。文件以 security 連線為示範。

**失敗徵兆**：
- 診斷行顯示送出的是「連線類型要選哪個」原句
- 憑空補出 D1 沒寫的其他安全模式（例如 Basic256Sha256）

---

## T3 — 極短追問，切換工具

> 那 Ignition 呢？

**預期分類**：`[FOLLOW_UP]`。這句指涉是開放的，可能是問整套流程，也可能延續 T2 只問連線類型。兩種解讀都可接受，**但診斷行必須顯示它選了哪一種**；若判定為指涉不明而反問一句，也是正確行為。

**應該出現**：D2 的內容。Ignition 的連線類型選項與 D1 相同。

**失敗徵兆**：沿用 T1 T2 已在 context 中的 UAexpert 文件直接作答而未重新檢索（這不是 IN_SCOPE，Ignition 是不同文件）。

---

## T4 — 跨文件污染（核心測試）

> Ignition 的預設帳號密碼是什麼？

**預期分類**：`[FOLLOW_UP]`。

**應該出現**：說明 D2 只指示要設定 username 與 password，**並未提供預設值**，屬於資訊不足，並依 Asking for Missing Information 引導下一步。

**失敗徵兆（最重要）**：回答 `root / 0000000`。這組值只出現在 D3（WebAccess），把它套到 Ignition 上就是跨文件拼接失敗，直接違反 Keep document boundaries。

---

## T5 — 同主題換文件

> 用 WebAccess 的話要設哪些參數？

**預期分類**：`[FOLLOW_UP]` 或 `[NEW_TOPIC]` 皆可接受，重點是有重新檢索。

**應該出現**：D3 的 Step 4——device type 選 OPCUA、maximal monitor item per communication 設為 200、username/password 為 root/0000000、Primary 填 ADAM-6350 的 IP，port 為 4840。前面步驟為建立 SCADA 專案、新增 node、新增 TCPIP port，後面為下載專案、啟動 Kernel、信任憑證、ViewDAQ 驗證。

**Security Policy / Security Mode / Authentication 的具體值**（`Basic128Rsa15` / `Sign` / UserName）**在 D3 的 Fig.8 描述裡**。給出這些值不算失敗，但應說明來自文件圖示。若該次檢索沒有回傳那個 chunk，正確說法是「本次檢索未回傳」，不是「文件未指定」。

**失敗徵兆**：
- 說「文件未指定 Security 參數」而未限定於本次檢索範圍。
- 把 root/0000000 講成出廠預設值而非文件範例。

**曾誤列**：「把信任憑證步驟混進 WebAccess 流程」——D3 的 Step7 本來就有這個步驟，不是污染。

---

## T6 — 免重查（IN_SCOPE）

> port 是多少？

**預期分類**：`[IN_SCOPE]`，不重新檢索，並指名來源為 D3。

**應該出現**：4840。

**失敗徵兆**：
- 顯示 `[FOLLOW_UP]` 並重新查詢（答案已在上一輪文件中，屬於過度保守）
- 答出 4840 以外的值

---

## T7 — 換題目，同產品線

> 我的模組設定在重開機之後會不見，要怎麼處理？

**預期分類**：`[FOLLOW_UP]` 或 `[NEW_TOPIC]` 皆可，必須重新檢索。

**應該出現**：D4——升級韌體至 V1.21 B07 或以上，透過 Browse 選擇韌體檔後按 Download，等待「Download file done!」出現。文中的重現情境是 DI12 的 "Keep counter value when power off" 與 "Enable digital filter" 設定在重新上電後被清除。

**失敗徵兆**：仍在講 OPC UA 或憑證。

---

## T8 — 版本精確度

> 我的韌體是 1.20 B12

**預期分類**：`[FOLLOW_UP]`。

**應該出現**：D4 明確指認的問題版本是 **1.20 B13**，並建議升級到 1.21 B07 以上。對於 1.20 B12 是否受影響，文件沒有說。正確行為是點出這個落差，不對 B12 下結論。

**失敗徵兆**：
- 斷言「1.20 B12 不受影響」或「1.20 B12 也有這個問題」
- 把 B12 逕自當成 B13 處理

---

## T9 — 完全換主題（scope reset）

> BB-USH207-B 同時接多個高耗電裝置時會掉壓，為什麼？

**預期分類**：`[NEW_TOPIC]`，舊主題材料全部失效，以原問題檢索。

**應該出現**：D5——左三個 USB port 共用一顆 5V buck converter、右四個共用另一顆，各限流 7.5A。情境一把高耗電裝置集中在同一組，該顆需供應 3×2.4A + 1×1A = 8.2A，超過 7.5A 上限而掉壓。整機最大總負載 42W。解法是把高負載裝置分散到不同 port group。

**失敗徵兆**：任何 ADAM-6300、OPC UA、韌體版本的內容殘留。

---

## T10 — 換主題後的追問

> Linux 上要怎麼裝驅動？

**預期分類**：`[FOLLOW_UP]`，改寫應帶入 BB / ULI 系列 USB 轉換器。

**應該出現**：D6——先 `lsusb` 取得 VID/PID（範例為 VID 0856、PID bf02），在 `/etc/udev/rules.d` 建立 `99-usbftdi.rules`，寫入指定的 ACTION/SUBSYSTEMS/ATTRS 規則，執行 `sudo udevadm control --reload-rules` 與 `sudo udevadm trigger`，最後以 `dmesg | grep FTDI` 確認出現 "now attached to ttyUSB0"。文件環境為 Ubuntu 20.04.4、kernel 5.15.0。

**失敗徵兆**：
- 把 T9 的 buck converter 內容混進來
- 改寫成與 BB/ULI 無關的泛用 Linux 驅動問題
- 更動 VID/PID 或 udev 規則的字串內容

---

## T11 — 已捨棄主題的回溯（邊界案例）

> 剛剛講的那個 ADAM 韌體版本是多少？

這一輪沒有標準答案，用來觀察規則在邊界上的表現。ADAM 主題已在 T9 被捨棄，但使用者明確要求回溯。

**可接受**：重新檢索後回答；或明確說明該主題已切換、要求確認後再處理。

**失敗徵兆**：直接從已捨棄的舊 context 撈出版本號作答，卻宣稱是經過查證的。

---

## 評分重點

| 項目 | 對應輪次 |
|---|---|
| 縮寫追問是否正確改寫 | T2 T3 T10 |
| 跨文件是否污染 | T4 T5 T10 |
| 換主題是否確實重置 | T9 |
| 免重查是否恰當 | T6 |
| 是否過度推論版本 | T8 |
| 資訊不足時的引導品質 | T4 |
| 診斷行是否每輪都出現且正確 | 全部 |
| 缺失是否限定在本次檢索 | T5 T8 |
| 圖示來源的值是否標明出處 | T1 T5 |

---

## 查驗檢索紀錄

建立 `skills/foundry-iq/logs/` 目錄即啟用紀錄，刪除即停用。

每一輪的查詢句、回傳文件數與來源：

```bash
python3 -c "
import json
for i, line in enumerate(open('foundry_iq_retrievals.jsonl', encoding='utf-8'), 1):
    r = json.loads(line)
    print(f\"--- #{i}  {r['timestamp'][:19]}\")
    print('   問：', r['question'])
    print('   文件數：', len(r['documents']))
    for ref in r['references']:
        print('       ', ref.get('reranker_score'), ref.get('source_name'))
"
```

某個字串是否真的來自檢索，以及它在文字層還是圖片描述：

```bash
python3 -c "
import json, re
pat = re.compile(r'要查的字串')
for line in open('foundry_iq_retrievals.jsonl', encoding='utf-8'):
    r = json.loads(line)
    print('===', r['question'])
    for j, d in enumerate(r['documents']):
        m = pat.search(d['content'])
        if not m: continue
        s, e = max(0, m.start()-150), m.end()+150
        kind = '圖片描述' if re.search(r'screenshot|shows |image|panel|dropdown|window', d['content'][:400], re.I) else '文字層'
        print(f'  chunk {j} [{kind}]')
        print('   ...' + d['content'][s:e].replace(chr(10),' ') + '...')
"
```

零命中才代表是模型生成的。有命中就要再看它落在文字層還是圖片描述。
