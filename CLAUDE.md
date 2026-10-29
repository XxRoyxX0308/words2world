# words2world

文字 / 圖片 → Unreal 場景。工作分兩個輪流進行的階段，各有一個 skill：

- `/world-forge`（.claude/skills/world-forge）：GPU 階段。概念圖、物件清單與變體、2D 迭代、去背、TRELLIS 1024。**Blender 與 Unreal 必須關閉。**
- `/world-build`（.claude/skills/world-build）：引擎階段。烘焙、建築、地形植被、擺放、貼花、燈光、截圖審查。**不能跑 TRELLIS 或去背**；ComfyUI 只用 `--lowvram`。

不確定在哪個階段：`python w2w.py status <world>` 與 `python w2w.py handoff <world>`。
判斷標準：docs/ART_RULES.md；格式：docs/FORMATS.md。

硬性限制：
- 顯卡 12 GB：ComfyUI、TRELLIS / RMBG、Blender、Unreal 不同時佔用（指令會檢查）。
- 不在 conda env `trellis2` 安裝或升級套件；不印出 `~/.cache/huggingface/token`；FLUX 只用 klein 4B。
- 不要用會比對到自己的 `pkill -f` / `pgrep -f`（樣式第一個字母包成 `[x]`）。
- Unreal 只寫 `/Game/W2W`；第一次對某專案匯入前先告訴使用者。下載材質前告訴使用者來源與大小。
- 看圖的步驟（候選圖、去背、preview、截圖）一定親自 Read；只回報看過、驗證過的結果。
- 每個階段結束時在對話裡寫交接 prompt（不寫成檔案）。
