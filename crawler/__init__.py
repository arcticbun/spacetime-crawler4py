from utils import get_logger
from crawler.frontier import Frontier
from crawler.worker import Worker

class Crawler(object):
    def __init__(self, config, restart, frontier_factory=Frontier, worker_factory=Worker):
        self.config = config
        self.logger = get_logger("CRAWLER")
        self.frontier = frontier_factory(config, restart)
        self.workers = list()
        self.worker_factory = worker_factory

    def start_async(self):
        self.workers = [
            self.worker_factory(worker_id, self.config, self.frontier)
            for worker_id in range(self.config.threads_count)]
        for worker in self.workers:
            worker.start()

    def start(self):
        try:
            if self.config.threads_count != 1:
                raise ValueError("This crawler currently supports THREADCOUNT = 1 only")
            worker = self.worker_factory(0, self.config, self.frontier)
            worker.run()
        finally:
            self.frontier.save.close()

    def join(self):
        for worker in self.workers:
            worker.join()
