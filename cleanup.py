"""
清理腳本 — 刪除測試人選 + 非台灣人選
"""
import time
import gspread
from google.oauth2.service_account import Credentials

SCOPES = [
    'https://www.googleapis.com/auth/spreadsheets',
    'https://www.googleapis.com/auth/drive',
]

SPREADSHEET_ID = '15X2NNK9bSmSl-GfCmfO2q8lS2wAinR9fNMZr4vqrMug'
CREDENTIALS_FILE = 'credentials.json'

# 台灣相關的 location 關鍵字
TW_KEYWORDS = [
    'taiwan', 'taipei', '台灣', '台北',
    'hsinchu', '新竹', 'taichung', '台中',
    'kaohsiung', '高雄', 'tainan', '台南',
    'taoyuan', '桃園', 'keelung', '基隆',
    'changhua', '彰化', 'pingtung', '屏東',
    'yilan', '宜蘭', 'nantou', '南投',
    'miaoli', '苗栗', 'chiayi', '嘉義',
    'hualien', '花蓮', 'taitung', '台東',
    'tw', 'r.o.c',
]

# 測試用的工作表名稱
TEST_SHEETS = ['測試客戶', '測試-搜尋優化', '測試-地區優化', '候選人工作表範本']


def is_taiwan(location: str) -> bool:
    """判斷是否為台灣地區"""
    if not location or not str(location).strip():
        return True  # 空 location 保留（可能是台灣但沒填）
    loc = str(location).lower().strip()
    return any(kw in loc for kw in TW_KEYWORDS)


def main():
    print("=" * 60)
    print("Google Sheets 清理腳本")
    print("刪除測試工作表 + 非台灣候選人")
    print("=" * 60)

    # 連線
    creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=SCOPES)
    gc = gspread.authorize(creds)
    spreadsheet = gc.open_by_key(SPREADSHEET_ID)
    print(f"\n已連線: {spreadsheet.title}")

    # 列出所有工作表
    all_sheets = spreadsheet.worksheets()
    print(f"工作表數量: {len(all_sheets)}")
    for ws in all_sheets:
        print(f"  - {ws.title}")

    time.sleep(2)  # 避免 rate limit

    # === 1. 刪除測試工作表 ===
    print("\n" + "=" * 40)
    print("[步驟 1] 刪除測試工作表")
    print("=" * 40)

    deleted_sheets = []
    for ws in all_sheets:
        if ws.title in TEST_SHEETS:
            try:
                spreadsheet.del_worksheet(ws)
                deleted_sheets.append(ws.title)
                print(f"  ✅ 已刪除工作表: {ws.title}")
                time.sleep(1)
            except Exception as e:
                print(f"  ❌ 刪除失敗 {ws.title}: {e}")

    if not deleted_sheets:
        print("  沒有找到測試工作表")

    time.sleep(2)

    # === 2. 清理非台灣候選人 ===
    print("\n" + "=" * 40)
    print("[步驟 2] 清理非台灣候選人")
    print("=" * 40)

    # 重新載入工作表列表（扣除已刪除的）
    remaining_sheets = spreadsheet.worksheets()
    processed_sheet = '去重'

    total_deleted_rows = 0

    for ws in remaining_sheets:
        if ws.title == processed_sheet:
            continue  # 跳過去重紀錄表

        print(f"\n  --- 處理工作表: {ws.title} ---")
        time.sleep(2)  # 避免 rate limit

        try:
            all_values = ws.get_all_values()
        except Exception as e:
            print(f"    ❌ 讀取失敗: {e}")
            time.sleep(5)
            continue

        if len(all_values) <= 1:
            print(f"    空工作表，跳過")
            continue

        header = all_values[0]
        data_rows = all_values[1:]

        # 找 location 欄位
        try:
            loc_idx = header.index('location')
        except ValueError:
            print(f"    找不到 location 欄位，跳過")
            continue

        # 也找 name 欄位方便 log
        try:
            name_idx = header.index('name')
        except ValueError:
            name_idx = None

        # 找出非台灣的 row indexes（從底部開始刪，避免 index 偏移）
        non_tw_rows = []
        for i, row in enumerate(data_rows):
            location = row[loc_idx] if loc_idx < len(row) else ''
            if not is_taiwan(location):
                name = row[name_idx] if name_idx is not None and name_idx < len(row) else '?'
                non_tw_rows.append((i + 2, name, location))  # +2 因為 header + 0-based

        if not non_tw_rows:
            print(f"    全部都是台灣人選 ({len(data_rows)} 筆)")
            continue

        print(f"    共 {len(data_rows)} 筆，非台灣: {len(non_tw_rows)} 筆")
        for row_num, name, loc in non_tw_rows:
            print(f"      Row {row_num}: {name} | {loc}")

        # 從底部往上刪（避免 row index 偏移）
        non_tw_rows.reverse()
        for row_num, name, loc in non_tw_rows:
            try:
                ws.delete_rows(row_num)
                print(f"      ✅ 已刪除: {name} ({loc})")
                total_deleted_rows += 1
                time.sleep(1.5)  # 避免 rate limit
            except Exception as e:
                print(f"      ❌ 刪除失敗 Row {row_num}: {e}")
                time.sleep(3)

    # === 3. 清理去重表中的測試紀錄 ===
    print("\n" + "=" * 40)
    print("[步驟 3] 清理去重紀錄中的測試客戶")
    print("=" * 40)

    time.sleep(2)
    try:
        dedup_ws = spreadsheet.worksheet(processed_sheet)
        all_values = dedup_ws.get_all_values()

        if len(all_values) > 1:
            header = all_values[0]
            try:
                client_idx = header.index('client_name')
            except ValueError:
                print("  找不到 client_name 欄位")
                client_idx = None

            if client_idx is not None:
                test_rows = []
                for i, row in enumerate(all_values[1:]):
                    client = row[client_idx] if client_idx < len(row) else ''
                    if client in TEST_SHEETS:
                        test_rows.append((i + 2, client))

                if test_rows:
                    print(f"  找到 {len(test_rows)} 筆測試紀錄")
                    test_rows.reverse()
                    for row_num, client in test_rows:
                        try:
                            dedup_ws.delete_rows(row_num)
                            print(f"    ✅ 已刪除去重紀錄: Row {row_num} ({client})")
                            time.sleep(1.5)
                        except Exception as e:
                            print(f"    ❌ 刪除失敗: {e}")
                            time.sleep(3)
                else:
                    print("  沒有測試紀錄")
        else:
            print("  去重表是空的")
    except Exception as e:
        print(f"  ❌ 處理去重表失敗: {e}")

    # === 結果 ===
    print("\n" + "=" * 60)
    print("清理完成!")
    print(f"  刪除測試工作表: {len(deleted_sheets)} 個")
    print(f"  刪除非台灣候選人: {total_deleted_rows} 筆")
    print("=" * 60)


if __name__ == '__main__':
    main()
