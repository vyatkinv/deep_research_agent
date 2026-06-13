"""Auto-skill router — определяет подходящий скилл по тексту сообщения.

Использует keyword-matching по регулярным выражениям.
Возвращает None если ни один скилл не подходит (общий агент без скилла).
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from config import SkillConfig

# Паттерны для каждого скилла.
# Чем больше паттернов совпало — тем выше приоритет.
# Порядок в словаре не важен — выбирается максимальный score.
_PATTERNS: Dict[str, List[str]] = {
    "crud_generator": [
        r"crud",
        r"создай.{0,20}entity",
        r"сгенерир.{0,20}entity",
        r"генер.{0,20}crud",
        r"создай.{0,30}(controller|контроллер|репозитор|сервис.{0,10}entity)",
        r"полный стек",
        r"rest.*entity",
    ],
    "fix_build": [
        r"(ошибк|error).{0,20}компил",
        r"компил.{0,20}(ошибк|error|fail)",
        r"не компилируется",
        r"cannot find symbol",
        r"fix.{0,10}build",
        r"build.{0,10}(error|fail|broken)",
        r"исправь.{0,20}компил",
        r"compilation.{0,10}fail",
    ],
    "fix_tests": [
        r"тест.{0,20}(упал|падает|fail|broken)",
        r"(упал|падает|fail).{0,20}тест",
        r"fix.{0,10}test",
        r"исправь.{0,20}тест",
        r"failing.{0,10}test",
        r"тест.{0,10}не проход",
    ],
    "spring_boot": [
        r"spring[\s_-]?boot",
        r"spring[\s_-]?(security|data|web|mvc|cloud)",
        r"@restcontroller",
        r"@springbootapplication",
        r"jwt.{0,20}(auth|spring|token)",
        r"actuator",
        r"(application|bootstrap)\.(yml|yaml|properties)",
        r"@configurationproperties",
        r"@enablewebsecurity",
    ],
    "code_review": [
        r"\bревью\b",
        r"code[\s_-]?review",
        r"проверь.{0,15}код",
        r"review.{0,15}code",
        r"качество.{0,10}код",
        r"code.{0,10}quality",
        r"посмотри.{0,20}(на код|класс|файл)",
        r"что не так.{0,10}(с|в).{0,10}код",
    ],
    "explain_code": [
        r"\bобъясни\b",
        r"\bexplain\b",
        r"как работает",
        r"what does.{0,30}do",
        r"расскажи.{0,20}(о|про|как).{0,20}(класс|метод|код)",
        r"помоги понять",
        r"не понимаю.{0,20}(код|класс|метод)",
    ],
    "add_tests": [
        r"(напиши|добавь|создай).{0,20}тест",
        r"(write|add|create).{0,10}test",
        r"junit.{0,20}(для|to|for)",
        r"покрыт.{0,10}тест",
        r"test.{0,10}coverage",
        r"тестовое покрытие",
    ],
    "modernize": [
        r"\bмодерниз",
        r"\bmodernize\b",
        r"обнов.{0,20}java.{0,10}(стиль|код|синтаксис)",
        r"java\s*2[01]",
        r"(перепиши|convert).{0,20}record",
        r"sealed\s+class",
        r"pattern.{0,5}matching",
        r"рефактор.{0,20}современ",
    ],
    "extract_service": [
        r"вынеси.{0,20}(в сервис|логик|service)",
        r"extract.{0,10}service",
        r"(жирн|fat|толст).{0,20}(controller|контроллер)",
        r"контроллер.{0,20}(слишком много|too much)",
        r"single.{0,5}responsibility",
        r"srp.{0,10}(нарушен|violation)",
        r"разнеси.{0,20}(логику|код)",
    ],
    "generate_docs": [
        r"\bjavadoc\b",
        r"(написать|создать|сгенерир).{0,20}документаци",
        r"(generate|write).{0,10}(docs?|documentation)",
        r"\breadme\b",
        r"api.{0,10}документ",
        r"документируй",
    ],
    "dependency_audit": [
        r"аудит.{0,20}зависим",
        r"dependency.{0,10}audit",
        r"устарел.{0,20}(зависим|библиотек|версия)",
        r"(обнов|upgrade).{0,20}зависим",
        r"уязвимост",
        r"vulnerability",
        r"owasp",
        r"versions.{0,10}(plugin|обнов)",
    ],
    "design_pattern": [
        r"(паттерн|pattern).{0,20}(применить|использовать|apply)",
        r"(factory|strategy|observer|decorator|singleton|builder|adapter|facade|command|state)",
        r"(применить|использовать).{0,20}паттерн",
        r"design.{0,5}pattern",
    ],
    "find_library": [
        r"(найди|подбери|посоветуй).{0,20}библиотек",
        r"(find|recommend|suggest).{0,10}library",
        r"какую библиотек",
        r"(добавь|add).{0,15}(зависимост|dependency).{0,10}(для|for)",
        r"лучш.{0,10}библиотек.{0,20}(для|to|for)",
        r"альтернатив.{0,20}библиотек",
    ],
}

# Компилируем паттерны один раз при загрузке модуля
_COMPILED: Dict[str, List[re.Pattern]] = {
    name: [re.compile(p, re.IGNORECASE) for p in patterns]
    for name, patterns in _PATTERNS.items()
}


def route(
    message: str,
    registry: Dict[str, SkillConfig],
) -> Optional[SkillConfig]:
    """Выбрать наиболее подходящий скилл для сообщения.

    Args:
        message: Текст пользовательского сообщения.
        registry: Словарь name → SkillConfig доступных скиллов.

    Returns:
        SkillConfig победителя или None если ни один не подошёл.
    """
    scores: List[Tuple[int, str]] = []

    for skill_name, patterns in _COMPILED.items():
        if skill_name not in registry:
            continue
        score = sum(1 for p in patterns if p.search(message))
        if score > 0:
            scores.append((score, skill_name))

    if not scores:
        return None

    scores.sort(key=lambda x: x[0], reverse=True)
    best_name = scores[0][1]
    return registry[best_name]


def score_all(
    message: str,
    registry: Dict[str, SkillConfig],
) -> List[Tuple[str, int]]:
    """Вернуть список (skill_name, score) для всех скиллов с ненулевым совпадением.
    Используется для отладки и объяснения выбора.
    """
    result = []
    for skill_name, patterns in _COMPILED.items():
        if skill_name not in registry:
            continue
        score = sum(1 for p in patterns if p.search(message))
        if score > 0:
            result.append((skill_name, score))
    return sorted(result, key=lambda x: x[1], reverse=True)
