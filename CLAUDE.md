# words2world

文字 / 圖片 → Unreal 場景。整個流程是一個 skill：`/world`（.claude/skills/world），依序跑四個階段，需要時回到 2：

1. 規劃與初步建築（Blender、Unreal）
2. 概念圖與 2D（ComfyUI）
3. 去背與 3D（RMBG、TRELLIS 1024：整張顯卡）
4. 營造場景（Blender、Unreal、ComfyUI `--lowvram`）

不確定在哪個階段：`python w2w.py status <world>` 與 `python w2w.py handoff <world>`（`next_phase`）。
判斷標準：docs/ART_RULES.md；格式：docs/FORMATS.md。

硬性限制：
- 顯卡 12 GB：ComfyUI、TRELLIS / RMBG、Blender、Unreal 不同時佔用。指令會自己切換（等 w2w 開的無介面程序、停 ComfyUI、自動 `--lowvram`），但使用者開的 Blender / Unreal 視窗只會拒絕，不能關掉。
- 不在 conda env `trellis2` 安裝或升級套件；不印出 `~/.cache/huggingface/token`；FLUX 只用 klein 4B。
- 不要用會比對到自己的 `pkill -f` / `pgrep -f`（樣式第一個字母包成 `[x]`）。
- Unreal 只寫 `/Game/W2W`；第一次對某專案匯入前先告訴使用者。下載材質前告訴使用者來源與大小。
- 看圖的步驟（候選圖、去背、preview、截圖）一定親自 Read；只回報看過、驗證過的結果。
- 每個階段開始前跑 `python w2w.py context`；`CONTEXT_FULL` 就不開始該階段，在對話裡寫交接 prompt（不寫成檔案）。
