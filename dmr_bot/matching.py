"""Explainable heuristic matching for ordinary civilian goods; scores are not probabilities."""
from dataclasses import dataclass
import re
import unicodedata

STOP = {'и', 'с', 'со', 'в', 'во', 'на', 'для', 'по', 'от', 'до', 'из', 'к', 'а', 'или', 'под', 'при', 'не', 'без'}
GENERAL = {'изделие', 'устройство', 'блок', 'комплект', 'станция', 'система', 'модуль', 'товар'}
RADIO = re.compile(r'раци[яиюй]|радиостанц|walkie|two[ -]way|digital radio', re.I)
ACCESSORY = re.compile(r'антенн|аккумулятор|батаре|чехол|кабел|зарядн|гарнитур|клипс|программатор', re.I)
TOKEN = re.compile(r'[a-zа-я0-9]+(?:[.\-/][a-zа-я0-9]+)*')


def normalize(text):
    text = unicodedata.normalize('NFKC', text).casefold().replace('ё', 'е')
    return re.sub('[‐‑‒–—−]', '-', text)


def stem(word):
    # Conservative Russian inflection handling; no fuzzy matching of identifiers.
    for ending in ('ющими', 'ющего', 'ющему', 'ение', 'ения', 'ений', 'ного', 'ными', 'ный', 'ная', 'ное', 'ные', 'ной', 'ого', 'ыми', 'ими', 'ами', 'ями', 'ах', 'ях', 'ов', 'ев', 'ом', 'ем', 'ую', 'ая', 'ые', 'ой', 'ий', 'ый', 'а', 'я', 'ы', 'и', 'у', 'е'):
        if word.endswith(ending) and len(word) - len(ending) >= 4:
            return word[:-len(ending)]
    return word


@dataclass(frozen=True)
class Feature:
    text: str
    key: str
    weight: float
    identifier: bool = False


def features(query):
    tokens = TOKEN.findall(normalize(query))
    merged = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if (i + 1 < len(tokens) and re.fullmatch(r'[a-zа-я]{1,6}', token)
                and tokens[i + 1][0].isdigit() and token not in STOP):
            token += '-' + tokens[i + 1]
            i += 1
        merged.append(token)
        i += 1
    found = {}
    for token in merged:
        if token in STOP:
            continue
        identifier = any(c.isdigit() for c in token)
        # Compound adjectives are independent meaningful parts, not one exact phrase.
        parts = [token] if identifier else token.replace('-', ' ').split()
        for part in parts:
            key = re.sub(r'[^a-zа-я0-9]', '', part) if identifier else stem(part)
            weight = 8.0 if identifier else (0.5 if stem(part) in {stem(w) for w in GENERAL} else 2.0)
            if len(part) == 1 and not identifier:
                continue
            found[key] = Feature(part, key, weight, identifier)
    return list(found.values())


def identifier_matches(identifier, text):
    # Separate letters and digit runs; punctuation and spacing may vary, digits may not.
    parts = re.findall(r'[a-zа-я]+|[0-9]+', normalize(identifier))
    pattern = r'(?<![a-zа-я0-9])' + r'[\s.\-_/]*'.join(map(re.escape, parts)) + r'(?![a-zа-я0-9]|[.\-_/][0-9])'
    return bool(re.search(pattern, normalize(text)))


def evaluate(title, query):
    fs = features(query)
    title_words = {stem(w) for w in TOKEN.findall(normalize(title).replace('-', ' '))}
    matched = [f for f in fs if (identifier_matches(f.text, title) if f.identifier else f.key in title_words)]
    total = sum(f.weight for f in fs)
    score = sum(f.weight for f in matched)
    identifiers = [f for f in fs if f.identifier]
    exact_model = any(f.identifier for f in matched)
    meaningful = [f for f in matched if f.weight >= 2]
    # Two substantial words can qualify without a model, but generic nouns alone cannot.
    minimum_words = min(2, len([f for f in fs if not f.identifier and f.weight >= 2]))
    required = 2 if identifiers else minimum_words
    accepted = exact_model or (required >= 1 and len(meaningful) >= required and score >= 2)
    # With a requested model, a different explicit model is a contradiction.
    title_ids = [f for f in features(title) if f.identifier]
    def explicit_model(feature):
        # A voltage/size alone is not evidence of another model.
        value = feature.key
        if re.fullmatch(r'[0-9]+(?:в|а|вт|мм|см|м|кг|гц|v|w|hz)', value):
            return False
        return bool(re.search(r'[a-zа-я]', value) and re.search(r'[0-9]', value)) or bool(re.search(r'[.\-/]', feature.text))
    conflict = bool(identifiers and not exact_model and any(explicit_model(found) for found in title_ids))
    if conflict:
        accepted = False
    if ('dmr' in normalize(query) or RADIO.search(query)):
        accepted = accepted and bool(RADIO.search(title)) and not ACCESSORY.search(title)
    coverage = round(100 * score / total) if total else 0
    return {'accepted': bool(accepted), 'score': score, 'coverage': coverage,
            'matched': [f.text for f in matched], 'exact_model': exact_model,
            'model_conflict': conflict}


def query_variants(query):
    fs = features(query)
    variants = [query]
    models = [f.text for f in fs if f.identifier]
    words = [f.text for f in fs if not f.identifier and f.weight >= 2]
    if models:
        variants.append(' '.join(models))
        # Broader descriptions can find listings omitting the model.
        if len(words) >= 2:
            variants.append(' '.join(words[:3]))
    elif len(words) > 2:
        variants.append(' '.join(words[:2]))
    return list(dict.fromkeys(variants))[:3]
