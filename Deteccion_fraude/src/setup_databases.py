import pymysql
import pymongo
import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import MYSQL_CONFIG, MONGODB_CONFIG

def setup_mysql():
    print("Configurando MySQL...")
    connection = pymysql.connect(
        host=MYSQL_CONFIG['host'],
        user=MYSQL_CONFIG['user'],
        password=MYSQL_CONFIG['password'],
        port=MYSQL_CONFIG['port']
    )
    cursor = connection.cursor()
    
    cursor.execute(f"DROP DATABASE IF EXISTS {MYSQL_CONFIG['database']}")
    cursor.execute(f"CREATE DATABASE {MYSQL_CONFIG['database']}")
    cursor.execute(f"USE {MYSQL_CONFIG['database']}")
    
    cursor.execute("""
        CREATE TABLE transacciones (
            transaction_id VARCHAR(50) PRIMARY KEY,
            user_id VARCHAR(50),
            amount DECIMAL(15,2),
            timestamp DATETIME,
            merchant_category VARCHAR(50),
            is_fraud TINYINT(1),
            fraud_probability DECIMAL(5,4)
        )
    """)
    
    connection.commit()
    cursor.close()
    connection.close()
    print("✅ MySQL configurado")

def setup_mongodb():
    print("Configurando MongoDB...")
    client = pymongo.MongoClient(f"mongodb://{MONGODB_CONFIG['host']}:{MONGODB_CONFIG['port']}/")
    db = client[MONGODB_CONFIG['database']]
    
    db['transaction_logs'].create_index("transaction_id", unique=True)
    db['fraud_alerts'].create_index("transaction_id")
    
    print("✅ MongoDB configurado")

if __name__ == "__main__":
    setup_mysql()
    setup_mongodb()
    print("\n✅ Bases de datos listas")