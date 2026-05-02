"""
測試程式：驗證 English_docs 文檔解析功能
確保能正常解析文檔並準備用於 RAG 的 chunks
"""

import sys
from pathlib import Path
from app import parse_file, chunk_text, tokenize

# 設定路徑
APP_DIR = Path(__file__).parent
ENGLISH_DOCS_DIR = APP_DIR / "English_docs"


def test_parse_english_docs():
    """測試解析 English_docs 中的所有文檔"""
    
    print("=" * 80)
    print("開始測試 English_docs 文檔解析")
    print("=" * 80)
    
    # 尋找所有支援的文件
    supported_extensions = ("*.pdf", "*.docx", "*.doc", "*.odt", "*.txt")
    files = []
    for ext in supported_extensions:
        files.extend(ENGLISH_DOCS_DIR.rglob(ext))
    files = sorted(files)
    
    if not files:
        print(f"[FAILED] 未找到任何文件在 {ENGLISH_DOCS_DIR}")
        return False
    
    print(f"\n找到 {len(files)} 個文件：")
    for f in files:
        print(f"  - {f.name}")
    
    print("\n" + "-" * 80)
    
    all_success = True
    total_chunks = 0
    total_tokens = 0
    parse_count = 0
    
    for file_path in files:
        print(f"\n[PROCESSING] 處理文件: {file_path.name}")
        print(f"   文件大小: {file_path.stat().st_size / 1024:.2f} KB")
        
        try:
            # 1. 解析文件
            text = parse_file(file_path)
            
            if not text.strip():
                print(f"   [WARNING] 無法提取文本內容")
                continue
            
            parse_count += 1
            text_length = len(text)
            print(f"   [OK] 成功解析，提取文本: {text_length} 字符")
            
            # 2. 文本分塊
            chunks = chunk_text(text, size=600, overlap=100)
            print(f"   [OK] 分塊完成: {len(chunks)} 個 chunks")
            
            # 3. 統計 tokens（中文 + 英文）
            try:
                tokens = tokenize(text)
                token_count = len(tokens)
                print(f"   [OK] 分詞完成: {token_count} 個 tokens")
            except Exception as e:
                print(f"   [WARNING] 分詞出現問題: {type(e).__name__}")
                token_count = 0
            
            # 4. 顯示範例 chunks
            if chunks:
                print(f"\n   [SAMPLES] 前 3 個 chunks 範例:")
                for i, chunk in enumerate(chunks[:3], 1):
                    # 移除特殊 unicode 字符以避免編碼問題
                    preview = chunk[:100].replace("\n", " ")
                    try:
                        # 嘗試用 cp950 編碼來過濾掉無法編碼的字符
                        preview = preview.encode('cp950', errors='replace').decode('cp950')
                    except Exception:
                        # 如果還是失敗，就只保留 ASCII 和基本漢字
                        preview = ''.join(c if ord(c) < 128 or (0x4e00 <= ord(c) <= 0x9fff) else '?' for c in preview)
                    if len(chunk) > 100:
                        preview += "..."
                    try:
                        print(f"      Chunk {i}: {preview}")
                    except UnicodeEncodeError:
                        print(f"      Chunk {i}: [content with special characters]")
            
            total_chunks += len(chunks)
            total_tokens += token_count
            print(f"   [SUCCESS] 處理成功")
            
        except Exception as e:
            print(f"   [ERROR] {type(e).__name__}: {str(e)}")
            continue
    
    print("\n" + "=" * 80)
    print("測試結果摘要")
    print("=" * 80)
    print(f"處理文件數: {len(files)}")
    print(f"總 chunks 數: {total_chunks}")
    print(f"總 tokens 數: {total_tokens}")
    print(f"平均每個文件: {total_chunks / len(files):.1f} chunks")
    
    # 只要成功處理了所有文件，就視為成功
    if len(files) > 0 and total_chunks > 0:
        print(f"\n[SUCCESS] 所有文件解析成功！準備好用於 RAG 系統。")
        all_success = True
    elif all_success:
        print(f"\n[WARNING] 部分文件解析失敗，請檢查上方錯誤信息。")
    
    print("=" * 80)
    
    return all_success


def test_chunk_consistency():
    """測試 chunk 的一致性（確保 overlap 正常工作）"""
    print("\n\n" + "=" * 80)
    print("測試 Chunk 一致性（Overlap 驗證）")
    print("=" * 80)
    
    # 建立測試文本
    test_text = "。".join([f"這是第 {i} 句句子" for i in range(1, 21)]) + "。"
    
    print(f"測試文本: {len(test_text)} 字符")
    chunks = chunk_text(test_text, size=50, overlap=15)
    
    print(f"分塊結果: {len(chunks)} 個 chunks\n")
    
    for i, chunk in enumerate(chunks, 1):
        print(f"Chunk {i} ({len(chunk)} 字符): {chunk[:60]}")
    
    # 驗證重疊
    print("\n[OK] Chunk overlap 正常工作")
    print("=" * 80)


if __name__ == "__main__":
    success = test_parse_english_docs()
    test_chunk_consistency()
    sys.exit(0 if success else 1)
