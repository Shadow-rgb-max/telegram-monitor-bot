#!/bin/bash

# Скрипт для управления Docker контейнером tg-keyword-monitor
# Версия 2.1.0 с поддержкой дедупликации сообщений

CONTAINER_NAME="tg-keyword-monitor"
IMAGE_NAME="tg-keyword-monitor:latest"
CONFIG_DIR="/home/rovelin/tg-monitor-mum/tgkeyw-dmonitor-2.2.0-tg64"

# Цвета для вывода
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Функция для вывода цветного текста
print_status() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

print_header() {
    echo -e "${BLUE}=== $1 ===${NC}"
}

# Проверка наличия Docker
check_docker() {
    if ! command -v docker &> /dev/null; then
        print_error "Docker не установлен!"
        exit 1
    fi
}

# Проверка наличия файлов конфигурации
check_config() {
    if [ ! -f "$CONFIG_DIR/config.ini" ]; then
        print_error "Файл config.ini не найден в $CONFIG_DIR"
        exit 1
    fi
    
    if [ ! -f "$CONFIG_DIR/config.key" ]; then
        print_error "Файл config.key не найден в $CONFIG_DIR"
        exit 1
    fi
    
    print_status "Файлы конфигурации найдены"
}

# Проверка наличия образа
check_image() {
    if ! docker images | grep -q "$IMAGE_NAME"; then
        print_warning "Образ $IMAGE_NAME не найден. Создаю..."
        docker build -t "$IMAGE_NAME" .
        if [ $? -ne 0 ]; then
            print_error "Ошибка создания образа!"
            exit 1
        fi
    fi
}

# Запуск контейнера
start() {
    print_header "Запуск tg-keyword-monitor"
    
    check_docker
    check_config
    check_image
    
    # Проверяем, не запущен ли уже контейнер
    if docker ps | grep -q "$CONTAINER_NAME"; then
        print_warning "Контейнер уже запущен!"
        return 0
    fi
    
    if [ ! -f "$CONFIG_DIR/bot_session.session" ]; then
        print_warning "Файл bot_session.session не найден. Основной бот может не авторизоваться."
        print_warning "Создайте сессию локально: python3 create_telethon_session.py"
    fi
    
    # Запускаем контейнер (bot_session.session создаётся скриптом create_telethon_session.py)
    docker run -d \
        --name "$CONTAINER_NAME" \
        --restart unless-stopped \
        -v "$CONFIG_DIR/config.ini:/app/config.ini" \
        -v "$CONFIG_DIR/config.key:/app/config.key" \
        -v "$CONFIG_DIR/bot_session.session:/app/bot_session.session" \
        "$IMAGE_NAME"
    
    if [ $? -eq 0 ]; then
        print_status "Контейнер успешно запущен!"
        print_status "Используйте 'docker logs $CONTAINER_NAME' для просмотра логов"
    else
        print_error "Ошибка запуска контейнера!"
        exit 1
    fi
}

# Остановка контейнера
stop() {
    print_header "Остановка tg-keyword-monitor"
    
    if docker ps | grep -q "$CONTAINER_NAME"; then
        docker stop "$CONTAINER_NAME"
        docker rm "$CONTAINER_NAME"
        print_status "Контейнер остановлен и удален"
    else
        print_warning "Контейнер не запущен"
    fi
}

# Перезапуск контейнера
restart() {
    print_header "Перезапуск tg-keyword-monitor"
    stop
    sleep 2
    start
}

# Просмотр логов
logs() {
    print_header "Логи tg-keyword-monitor"
    
    if docker ps | grep -q "$CONTAINER_NAME"; then
        docker logs -f "$CONTAINER_NAME"
    else
        print_error "Контейнер не запущен!"
        exit 1
    fi
}

# Статус контейнера
status() {
    print_header "Статус tg-keyword-monitor"
    
    if docker ps | grep -q "$CONTAINER_NAME"; then
        print_status "Контейнер запущен"
        docker ps | grep "$CONTAINER_NAME"
    else
        print_warning "Контейнер не запущен"
        
        # Проверяем, есть ли остановленный контейнер
        if docker ps -a | grep -q "$CONTAINER_NAME"; then
            print_warning "Найден остановленный контейнер"
            docker ps -a | grep "$CONTAINER_NAME"
        fi
    fi
    
    # Проверяем образ
    if docker images | grep -q "$IMAGE_NAME"; then
        print_status "Образ $IMAGE_NAME найден"
    else
        print_warning "Образ $IMAGE_NAME не найден"
    fi
}

# Тестирование конфигурации
test_config() {
    print_header "Тестирование конфигурации"
    
    check_docker
    check_config
    check_image
    
    docker run --rm \
        -v "$CONFIG_DIR/config.ini:/app/config.ini" \
        -v "$CONFIG_DIR/config.key:/app/config.key" \
        "$IMAGE_NAME" \
        python3 -c "
from config import get_config
config = get_config()
print('✅ Конфигурация загружена успешно!')
print(f'   Каналов для мониторинга: {len(config.channels)}')
print(f'   Ключевых слов: {len(config.keywords)}')
print(f'   Окно дедупликации: {config.dedup_window_hours} часов')
print(f'   Каналы: {\", \".join(config.channels)}')
print(f'   Ключевые слова: {\", \".join(config.keywords)}')
"
}

# Пересборка образа
rebuild() {
    print_header "Пересборка Docker образа"
    
    check_docker
    
    # Останавливаем контейнер если запущен
    if docker ps | grep -q "$CONTAINER_NAME"; then
        print_warning "Останавливаю запущенный контейнер..."
        stop
    fi
    
    # Удаляем старый образ
    if docker images | grep -q "$IMAGE_NAME"; then
        print_status "Удаляю старый образ..."
        docker rmi "$IMAGE_NAME"
    fi
    
    # Собираем новый образ
    print_status "Собираю новый образ..."
    docker build -t "$IMAGE_NAME" .
    
    if [ $? -eq 0 ]; then
        print_status "Образ успешно пересобран!"
        print_status "Используйте './docker-manager.sh start' для запуска"
    else
        print_error "Ошибка сборки образа!"
        exit 1
    fi
}

# Очистка
cleanup() {
    print_header "Очистка Docker ресурсов"
    
    # Останавливаем и удаляем контейнер
    if docker ps -a | grep -q "$CONTAINER_NAME"; then
        print_status "Удаляю контейнер..."
        docker rm -f "$CONTAINER_NAME" 2>/dev/null
    fi
    
    # Удаляем образ
    if docker images | grep -q "$IMAGE_NAME"; then
        print_status "Удаляю образ..."
        docker rmi "$IMAGE_NAME"
    fi
    
    print_status "Очистка завершена"
}

# Показать справку
show_help() {
    echo "Использование: $0 {start|stop|restart|status|logs|test|rebuild|cleanup|help}"
    echo ""
    echo "Команды:"
    echo "  start     - Запустить контейнер"
    echo "  stop      - Остановить контейнер"
    echo "  restart   - Перезапустить контейнер"
    echo "  status    - Показать статус"
    echo "  logs      - Показать логи (следить)"
    echo "  test      - Протестировать конфигурацию"
    echo "  rebuild   - Пересобрать Docker образ"
    echo "  cleanup   - Очистить все Docker ресурсы"
    echo "  help      - Показать эту справку"
    echo ""
    echo "Примеры:"
    echo "  $0 start    # Запустить бота"
    echo "  $0 logs     # Смотреть логи"
    echo "  $0 test     # Проверить конфигурацию"
}

# Основная логика
case "$1" in
    start)
        start
        ;;
    stop)
        stop
        ;;
    restart)
        restart
        ;;
    status)
        status
        ;;
    logs)
        logs
        ;;
    test)
        test_config
        ;;
    rebuild)
        rebuild
        ;;
    cleanup)
        cleanup
        ;;
    help|--help|-h)
        show_help
        ;;
    *)
        print_error "Неизвестная команда: $1"
        echo ""
        show_help
        exit 1
        ;;
esac 
