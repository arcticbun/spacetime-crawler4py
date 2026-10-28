from utils import get_logger
from crawler.frontier import Frontier
from crawler.worker import Worker

class Crawler(object):
    def __init__(self, config, restart, frontier_factory=Frontier, worker_factory=Worker):
        self.config = config
        self.logger = get_logger("CRAWLER")
        self.frontier = frontier_factory(config, restart)
        self.worker_factory = worker_factory

    def start(self):
        """Runs a single worker in the current thread until the frontier is empty."""
        try:
            if self.config.threads_count != 1:
                raise ValueError("This crawler currently supports THREADCOUNT = 1 only")
            worker = self.worker_factory(0, self.config, self.frontier)
            worker.run()
        finally:
            self.frontier.save.close()
