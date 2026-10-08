import sqlite3
import os
from database import DB_PATH


def init_warehouses():
    """Инициализирует склады в БД из config.json"""
    import json
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)

    warehouses = config.get("warehouse", {}).get("warehouses", [])

    if not warehouses:
        print("❌ В config.json нет данных о складах")
        return

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    for wh in warehouses:
        cursor.execute("""
            INSERT OR REPLACE INTO warehouses 
            (key, name, capacity, orange_threshold, red_threshold, repeat_interval_min, current_value, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 0, 0)
        """, (
            wh["key"],
            wh["name"],
            wh["capacity"],
            wh["orange_threshold"],
            wh["red_threshold"],
            wh["repeat_interval_min"]
        ))
        print(f"✅ Добавлен склад: {wh['key']} - {wh['name']}")

    conn.commit()
    conn.close()
    print(f"\n✅ Инициализировано {len(warehouses)} складов в БД")


if __name__ == "__main__":
    init_warehouses()