from models.task import Task

from daos.base import BaseDao


class TaskDao(BaseDao):
    def save(self, task: Task) -> Task:
        self.session.add(task)
        self.session.commit()
        self.session.refresh(task)
        return task
