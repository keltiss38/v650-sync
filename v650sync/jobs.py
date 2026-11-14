"""Suivi d'une tâche de fond: étapes (liste de tâches avec une barre chacune), progression globale, journal visible dans la page.

Une tâche = une fonction `fn(log, progress)`. `progress` est appelable comme avant (`progress(fait, total)`) et sait en plus
annoncer des étapes: `progress.plan([(clé, libellé), ...])`, `progress.begin(clé)`, `progress.end(clé)`. Une fonction qui reçoit un
simple `lambda` (tests, ligne de commande) passe par `steps_of()`, qui rend un suivi qui ne fait rien.
"""
import time


class NullSteps:
    def plan(self, steps):
        pass

    def begin(self, key, label=None):
        pass

    def end(self, key=None):
        pass

    def label(self, key, label):
        pass

    def note(self, text):
        pass

    def __call__(self, done, total):
        pass


def steps_of(progress):
    return progress if hasattr(progress, "begin") else NullSteps()


def new_job():
    return {"id": 0, "running": False, "kind": None, "title": "", "log": [], "result": None, "error": None, "progress": [0, 0],
            "info": {}, "steps": [], "started": 0.0, "ended": 0.0, "note": ""}


class Progress:
    """Passé à la fonction de la tâche: met à jour le dictionnaire de la tâche lu par la page."""

    def __init__(self, job):
        self.job = job

    def _step(self, key):
        return next((s for s in self.job["steps"] if s["key"] == key), None)

    def plan(self, steps):
        self.job["steps"] = [{"key": k, "label": l, "state": "pending", "done": 0, "total": 0} for k, l in steps]

    def begin(self, key, label=None):
        for s in self.job["steps"]:  # une étape qui commence referme celles d'avant
            if s["state"] == "running":
                s["state"] = "done"
                s["done"] = s["total"] or s["done"]
        s = self._step(key)
        if s:
            s["state"] = "running"
            s["done"], s["total"] = 0, 0
            s["t0"] = time.time()  # pour estimer le temps restant de cette étape
            if label:
                s["label"] = label
        self.job["progress"] = [0, 0]

    def end(self, key=None):
        for s in self.job["steps"]:
            if s["state"] == "running" and (key is None or s["key"] == key):
                s["state"] = "done"
                s["done"] = s["total"] or s["done"]

    def label(self, key, label):
        s = self._step(key)
        if s:
            s["label"] = label

    def note(self, text):
        """Message de vie affiché sous la barre globale (l'étape en cours n'a pas de pourcentage)."""
        self.job["note"] = text

    def __call__(self, done, total):
        self.job["progress"] = [done, total]
        for s in self.job["steps"]:
            if s["state"] == "running":
                s["done"], s["total"] = done, total

    def finish(self, ok):
        """Fin de la tâche: succès = tout est fait ; échec = l'étape en cours est marquée en erreur, les suivantes restent en attente."""
        for s in self.job["steps"]:
            if ok:
                s["state"] = "done"
                s["done"] = s["total"] or s["done"]
            elif s["state"] == "running":
                s["state"] = "error"
        self.job["ended"] = time.time()
