# Configuración de bases de datos
MYSQL_CONFIG = {
    'host': 'localhost',
    'user': 'root',
    'password': 'root',
    'database': 'fraud_detection_db',
    'port': 3306
}

MONGODB_CONFIG = {
    'host': 'localhost',
    'port': 27017,
    'database': 'fraud_detection_db'
}

# Configuración de Spark
SPARK_CONFIG = {
    'app_name': 'FraudDetection_UNRC',
    'driver_memory': '4g',
    'executor_memory': '4g'
}