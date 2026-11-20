"""H11 similar cases: the nearest completed cases of a task's process, by what the task page shows of the case.

Privacy: only the variables named in the task's `context` (what the person sees) are compared and shown, and only
the task's own form fields are shown as the decision. Another case's other data never leaves it."""
import hashlib
import math
import re
from collections import Counter

from ..clients.embed import EmbedClient
from ..clients.http import ServiceUnavailable
from ..models import Case, CaseEmbedding, Event, MessageSubscription, UserTask

LIMIT, POOL = 5, 200  # shown; compared (the process's most recent cases)
WORD = re.compile(r"\w+")


def text_of(variables, keys):
    """The case's variables named in `keys`, as "key: value" lines (scalars only)."""
    return "\n".join(f"{k}: {variables[k]}" for k in sorted(keys) if isinstance(variables.get(k), (str, int, float, bool)))


def cosine(a, b):
    return sum(x * y for x, y in zip(a, b))


def tfidf(texts):
    """Unit TF-IDF vectors (dicts) of `texts`. ponytail: pure Python over at most POOL + 1 short texts."""
    docs = [Counter(WORD.findall(t.lower())) for t in texts]
    df = Counter(w for d in docs for w in d)
    vectors = []
    for d in docs:
        v = {w: n * (math.log((1 + len(docs)) / (1 + df[w])) + 1) for w, n in d.items()}
        norm = math.sqrt(sum(x * x for x in v.values())) or 1
        vectors.append({w: x / norm for w, x in v.items()})
    return vectors


def sparse_cosine(a, b):
    return sum(x * b.get(w, 0) for w, x in a.items())


def embeddings(cases, query):
    """(the vector of `query`, those of `cases` ([(case, text)])), cached per case and text: only the missing ones go
    to the models server. Raises ServiceUnavailable."""
    keys = {c.pk: hashlib.sha1(t.encode()).hexdigest() for c, t in cases}
    have = {(e.case_id, e.text_hash): e.vector for e in CaseEmbedding.objects.filter(
        case__in=[c for c, _ in cases], text_hash__in=set(keys.values()))}
    missing = [(c, t) for c, t in cases if (c.pk, keys[c.pk]) not in have]
    vectors = EmbedClient().embed([query, *(t for _, t in missing)])
    for (c, _), vector in zip(missing, vectors[1:]):
        have[(c.pk, keys[c.pk])] = vector
        CaseEmbedding.objects.get_or_create(case=c, text_hash=keys[c.pk], defaults={"vector": vector})
    return vectors[0], [have[(c.pk, keys[c.pk])] for c, _ in cases]


def similar_cases(task, user):
    """(the nearest completed cases of the task's process, how they were compared): [{case_id, execution, score, end,
    decision, link}], at most LIMIT, best first. A case is completed when nothing of it waits any more."""
    keys = set(task.context) if isinstance(task.context, dict) else set()
    query = text_of(task.context, keys) if keys else ""
    if not (task.case_id and task.process and query):
        return [], ""
    waiting = {*UserTask.objects.waiting().values_list("case_id", flat=True),
               *MessageSubscription.objects.waiting().values_list("case_id", flat=True)}
    pool = [c for c in Case.objects.filter(process=task.process, terminated__isnull=True).exclude(case_id=task.case_id)
            .exclude(case_id__in=waiting).order_by("-updated")[:POOL] if text_of(c.variables, keys)]
    if not pool:
        return [], ""
    texts = [text_of(c.variables, keys) for c in pool]
    try:
        vector, vectors = embeddings(list(zip(pool, texts)), query)
        scores, how = [cosine(vector, v) for v in vectors], "sentence embeddings"
    except (ServiceUnavailable, KeyError, TypeError, ValueError):  # the models server is not there: words instead
        query_vector, *vectors = tfidf([query, *texts])
        scores, how = [sparse_cosine(query_vector, v) for v in vectors], "word overlap"
    best = sorted(((s, c) for s, c in zip(scores, pool) if s > 0),
                  key=lambda sc: (-round(sc[0], 9), sc[1].case_id))[:LIMIT]  # ties: by case id, deterministic
    visible = set(UserTask.objects.for_user(user).filter(case_id__in=[c.case_id for _, c in best]).values_list("case_id", flat=True))
    decided = {f["name"] for f in task.form}
    found = []
    for score, c in best:
        done = UserTask.objects.filter(case_id=c.case_id, activity=task.activity, status=UserTask.COMPLETED).order_by("-completed").first()
        end = Event.objects.filter(case_id=c.case_id).order_by("-ended").values_list("activity", flat=True).first()
        found.append({"case_id": c.case_id, "execution": c.case_id.rpartition(":")[2], "score": round(score, 2), "end": end or "",
                      "decision": {k: v for k, v in (done.result or {}).items() if k in decided} if done else {},
                      "link": user.is_staff or c.case_id in visible})
    return found, how
