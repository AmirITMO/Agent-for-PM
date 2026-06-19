"""Seed: task titles + statuses from screenshots. No assignees, no fake hours."""
import asyncio
from agent3_pm.database import AsyncSessionLocal, init_db
from agent3_pm.models import TaskStatus
from agent3_pm import repository as repo


async def seed():
    await init_db()
    async with AsyncSessionLocal() as session:
        await repo.create_user(session, "Roman Vassiliev", position="CEO")
        await repo.create_user(session, "Амир Хайруллин", position="МОП")

        dev = await repo.create_project(session, "MarketAI Dev")
        marketing = await repo.create_project(session, "MarketAI Marketing")

        async def add(title, project_id, status, is_bug=False):
            await repo.create_task(session, title, project_id, status=status, is_bug=is_bug)

        # ── MarketAI Dev ──
        for t in [
            'Исправить "Налог для УСН на НДС"',
            'Не сходятся данные в финотчете и дашборде',
            'Статус загрузки данных',
            'Подсказки «Как считается?» у ключевых строк',
            'График на дашборде непонятный',
            'Дополнить себестоимость: закупка + расходы Sellers +...',
            'Период данных — увеличить с года',
            'Эталонный формат выгрузки в Excel',
            'Рефакторинг фронтенда для SEO',
            'Подключить Nano Banana в MAI GPT',
            'При открытии AI ассистента не скролится страница',
        ]:
            await add(t, dev.id, TaskStatus.BACKLOG)

        for t in [
            'Пересобираем РНП (поартикульно)',
            'Поправить формулы в Excel отчете',
            'Неверная логистика — нет привязки к складам и формулы доставки',
            'Добавить блок цен (изначальная цена, скидка ВБ, СПП, цена для...)',
            'Убрать колонку «Выкупы, шт.» из режима «На единицу»',
            'Переделать формат выгрузки в Excel — сейчас непонятный и неудобный',
        ]:
            await add(t, dev.id, TaskStatus.PLANNING)

        await add('БАГ Расчет Налогов ОПИУ Дашборд', dev.id, TaskStatus.TODO, is_bug=True)
        await add('ИЗМЕНЕНИЕ Во всех фильтрах добавить возможность выбора...', dev.id, TaskStatus.TODO)
        await add('ИЗМЕНЕНИЕ Убрать Переключатель "До СПП, После СПП, К..."', dev.id, TaskStatus.TODO)
        await add('Изменение — сделать, чтобы можно было выбирать диапазон дат в...', dev.id, TaskStatus.TODO)

        await add('БАГ Комиссия в ОПИУ - Дашборд (ark)', dev.id, TaskStatus.WIP, is_bug=True)
        await add('БАГ ОПИУ Дашборд некорректно считается маржинальная прибыль...', dev.id, TaskStatus.WIP, is_bug=True)
        await add('БАГ Сумма продаж в ОПИУ - Дашборд (ark)', dev.id, TaskStatus.WIP, is_bug=True)

        for t in [
            'Ребилд страницы с партнерами',
            'Новые Hero блоки по UTM',
            'Описание технологий для Сколково',
            'Увеличить пространство на сервере',
            'В детализации по товарам показываются лишние артикулы, которых нет в...',
            'Чистая прибыль и рентабельность — это одно и то же на экране,...',
            'Расчёт рекламы через плановый ДРР (от цены без СПП)',
            'Маржа и рентабельность — пересчитать по корректной формуле',
            'Контроль остатков + правки',
            'Правки APU Dashboard',
        ]:
            await add(t, dev.id, TaskStatus.DONE)

        for t in [
            'Добавить себестоимость в Отчёт о прибылях и убытках',
            'Заменить «Валовую прибыль» на «Маржинальную...»',
            'Пересобрать дашборд',
        ]:
            await add(t, dev.id, TaskStatus.APPROVED)
        await add('ИЗМЕНЕНИЕ — переработать блок с налогами', dev.id, TaskStatus.APPROVED, is_bug=True)

        # ── MarketAI Marketing ──
        for t in [
            'Запустить авитологов',
            'Найти 100 лидеров-сетевиков',
            'Запустить ИИшный ледоруб (колл центр). TrySasha',
            'Подключить человеческий колл центр Smarter',
            'Пообщаться с Eva robot',
        ]:
            await add(t, marketing.id, TaskStatus.BACKLOG)

        for t in [
            'Переделать посадочную страницу (3 шт), сделать А/Б тестирование',
            'Собрать аналитику по трафику только с объявлений',
            'Оцифровать данные по маркетингу',
            'Встретиться с ребятами "лиды из чатов тг"',
        ]:
            await add(t, marketing.id, TaskStatus.TODO)

        await add('Встретиться с Никитой Соседом по арбитражу трафика', marketing.id, TaskStatus.DONE)

    print("Seed data loaded successfully!")


if __name__ == "__main__":
    asyncio.run(seed())
