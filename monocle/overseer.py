import traceback
from asyncio import gather, Semaphore, sleep, Task, CancelledError
from datetime import datetime
from statistics import median
from sys import platform
from cyrandom import shuffle
from collections import deque
from itertools import dropwhile
from time import time, monotonic
from threading import Thread

from aiopogo import HashServer
from sqlalchemy.exc import OperationalError
from heapq import nlargest, nsmallest

from .db import SIGHTING_CACHE, MYSTERY_CACHE, FORT_CACHE, RAID_CACHE
from .utils import get_current_hour, dump_pickle, get_start_coords, get_bootstrap_points, randomize_point, best_factors, percentage_split
from .shared import get_logger, LOOP, run_threaded
from .accounts import get_accounts, Account
from . import bounds, db_proc, spawns, sanitized as conf
from .worker import Worker
from .worker30 import Worker30, ENCOUNTER_CACHE
from .worker_raider import WorkerRaider
from .notification import Notifier
from .weather import Weather
from .parks import Parks


ANSI = '\x1b[2J\x1b[H'
if platform == 'win32':
    try:
        from platform import win32_ver
        from distutils.version import LooseVersion
        if LooseVersion(win32_ver()[1]) >= LooseVersion('10.0.10586'):
            import ctypes
            ctypes.windll.kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
        else:
            from os import system
            ANSI = ''
    except Exception:
        from os import system
        ANSI = ''

BAD_STATUSES = (
    'FAILED LOGIN',
    'EXCEPTION',
    'NOT AUTHENTICATED',
    'KEY EXPIRED',
    'HASHING OFFLINE',
    'NIANTIC OFFLINE',
    'BAD REQUEST',
    'INVALID REQUEST',
    'CAPTCHA',
    'BANNED',
    'BENCHING',
    'REMOVING',
    'IP BANNED',
    'MALFORMED RESPONSE',
    'AIOPOGO ERROR',
    'MAX RETRIES',
    'HASHING ERROR',
    'PROXY ERROR',
    'TIMEOUT'
)


class Overseer:
    def __init__(self, manager):
        self.log = get_logger('overseer')
        self.workers = []
        self.manager = manager
        self.things_count = deque(maxlen=9)
        self.paused = False
        self.coroutines_count = 0
        self.skipped = 0
        self.visits = 0
        self.coroutine_semaphore = Semaphore(conf.COROUTINES_LIMIT, loop=LOOP)
        self.redundant = 0
        self.running = True
        self.all_seen = False
        self.idle_seconds = 0
        self.log.info('Overseer initialized')
        self.status_log_at = 0
        self.pokemon_found = ''
        self.login_semaphore = Semaphore(conf.SIMULTANEOUS_LOGINS, loop=LOOP)
        self.sim_semaphore = Semaphore(conf.SIMULTANEOUS_SIMULATION, loop=LOOP)

    def start(self, status_bar):
        self.captcha_queue = self.manager.captcha_queue()
        self.extra_queue = self.manager.extra_queue()
        if conf.MAP_WORKERS:
            self.worker_dict = self.manager.worker_dict()
        else:
            self.worker_dict = None

        self.account_dict = get_accounts()
        self.add_accounts_to_queue(self.account_dict, self.captcha_queue, self.extra_queue)

        for x in range(conf.GRID[0] * conf.GRID[1]):
            try:
                self.workers.append(Worker(worker_no=x,
                    overseer=self,
                    captcha_queue=self.captcha_queue,
                    account_queue=self.extra_queue,
                    worker_dict=self.worker_dict,
                    account_dict=self.account_dict))
            except Exception as e:
                self.log.error("Worker initialization error: {}", e)
                traceback.print_exc()
        self.log.info("Worker count: ({}/{})", len(self.workers), conf.GRID[0] * conf.GRID[1])

        db_proc.Weather = Weather
        db_proc.start()
        LOOP.call_later(10, self.update_count)
        LOOP.call_later(max(conf.SWAP_OLDEST, conf.MINIMUM_RUNTIME * 60), self.swap_oldest)
        LOOP.call_soon(self.update_stats)
        if status_bar:
            LOOP.call_soon(self.print_status)

    def add_accounts_to_queue(self, account_dict, captcha_queue, account_queue):
        for username, account in account_dict.items():
            account['username'] = username
            if account.get('banned') or account.get('warn') or account.get('sbanned'):
                continue
            if account.get('captcha'):
                captcha_queue.put(account)
            else:
                account_queue.put(account)

    def update_count(self):
        self.things_count.append(str(db_proc.count))
        self.pokemon_found = (
            'Pokemon found count (10s interval):\n'
            + ' '.join(self.things_count)
            + '\n')
        LOOP.call_later(10, self.update_count)

    def swap_oldest(self, interval=conf.SWAP_OLDEST, minimum=conf.MINIMUM_RUNTIME):
        if (not self.paused and
                conf.EXTRA_ACCOUNT_PERCENT > 0.0 and
                (Account.estimated_extra_accounts() > 0 or
                    not self.extra_queue.empty())):
            try:
                oldest, minutes = self.longest_running()
                if minutes > minimum:
                    LOOP.create_task(oldest.lock_and_swap(minutes))
            except StopIteration as e:
                pass
        LOOP.call_later(interval, self.swap_oldest)

    def print_status(self, refresh=conf.REFRESH_RATE):
        try:
            self._print_status()
        except CancelledError:
            return
        except Exception as e:
            self.log.exception('{} occurred while printing status.', e.__class__.__name__)
        self.print_handle = LOOP.call_later(refresh, self.print_status)

    async def exit_progress(self):
        while self.coroutines_count > 2:
            try:
                self.update_coroutines_count(simple=False)
                pending = len(db_proc)
                # Spaces at the end are important, as they clear previously printed
                # output - \r doesn't clean whole line
                print(
                    '{} coroutines active, {} DB items pending   '.format(
                        self.coroutines_count, pending),
                    end='\r'
                )
                await sleep(.5)
            except CancelledError:
                return
            except Exception as e:
                self.log.exception('A wild {} appeared in exit_progress!', e.__class__.__name__)

    def update_stats(self, refresh=conf.STAT_REFRESH, med=median, count=conf.GRID[0] * conf.GRID[1]):
        visits = []
        seen_per_worker = []
        after_spawns = []
        speeds = []

        for w in self.workers:
            after_spawns.append(w.after_spawn)
            seen_per_worker.append(w.total_seen)
            visits.append(w.visits)
            speeds.append(w.speed)

        try:
            account_stats = Account.stats()
            account_reasons = ', '.join(['%s: %s' % (k,v) for k,v in account_stats[1].items()])
            account_refresh = datetime.fromtimestamp(account_stats[0]).strftime('%Y-%m-%d %H:%M:%S')
            account_clean = account_stats[2].get('clean')
            account_test = account_stats[2].get('test')
            account30_clean = account_stats[2].get('clean30')
            account30_test = account_stats[2].get('test30')
        except Exception as e:
            self.log.error("Unexpected error in overseer.update_stats: {}", e)
            account_reasons = None
            account_refresh = None
            account_clean = None
            account_test = None
            account30_clean = None
            account30_test = None

        stats_template = (
            'Seen per worker: min {}, max {}, med {:.0f}\n'
            'Visits per worker: min {}, max {}, med {:.0f}\n'
            'Visit delay: min {:.1f}, max {:.1f}, med {:.1f}\n'
            'Speed: min {:.1f}, max {:.1f}, med {:.1f}\n'
            'Worker30: {}, jobs: {}, caches: {}, encounters: {}, visits: {}, skips: {}, late: {}, hash wastes: {}\n'
            'Extra accounts: {}, CAPTCHAs needed: {}\n'
            'Accounts (this instance) {} (refreshed: {})\n'
            'Accounts (DB-wide) fresh/clean: {}, hibernated: {}, (Lv.30) fresh/clean: {}, hibernated: {}\n'
            )
        try:
            self.stats = stats_template.format(
                min(seen_per_worker), max(seen_per_worker), med(seen_per_worker),
                min(visits), max(visits), med(visits),
                min(after_spawns), max(after_spawns), med(after_spawns),
                min(speeds), max(speeds), med(speeds),
                len(Worker30.workers), Worker30.job_queue.qsize(), len(ENCOUNTER_CACHE),
                Worker30.encounters, Worker30.visits,
                Worker30.skipped, Worker30.lates, Worker30.hash_burn,
                self.extra_queue.qsize(), self.captcha_queue.qsize(),
                account_reasons, account_refresh,
                account_clean, account_test,
                account30_clean, account30_test,
            )
        except Exception as e:
            self.stats = stats_template.format(
                0, 0, 0,
                0, 0, 0,
                0, 0, 0,
                0, 0, 0,
                0, 0, 0,
                0, 0,
                0, 0, 0,
                0, 0,
                None, None,
                0, 0,
                0, 0
            )

        if Worker.has_raiders:
            smallest = nsmallest(1, WorkerRaider.job_queue.queue)
            if len(smallest) > 0:
                oldest_gym_raided = int(time() - smallest[0][0]) if len(smallest) > 0 else 0
            else:
                oldest_gym_raided = None
            self.stats += 'Raider workers: {}, gyms: {}, queue: {}, oldest: {}s\n'.format(
                len(WorkerRaider.workers), len(WorkerRaider.gyms),
                WorkerRaider.job_queue.qsize(), oldest_gym_raided
            )

        self.update_coroutines_count()
        counts_template = (
            'Known spawns: {}, unknown: {}, more: {}\n'
            'workers: {}, coroutines: {}\n'
            'sightings cache: {}, mystery cache: {}, DB queue: {}\n'
        )
        try:
            self.counts = counts_template.format(
                len(spawns), len(spawns.unknown), spawns.cells_count,
                count, self.coroutines_count,
                len(SIGHTING_CACHE), len(MYSTERY_CACHE), len(db_proc)
            )
        except Exception as e:
            self.counts = counts_template.format(
                0, 0, 0,
                0, 0,
                0, 0, 0
            )

        if self.status_log_at < time() - 15.0:
            self.status_log_at = time()
            visits =  """Visits: {}, Skipped: {}, Unnecessary: {}""".format(
                    self.visits,
                    self.skipped, self.redundant)
            for line in self.stats.split('\n'):
                if line.strip():
                    self.log.info(line)
            for line in self.counts.split('\n'):
                if line.strip():
                    self.log.info(line)
            for line in visits.split('\n'):
                if line.strip():
                    self.log.info(line)
            self.log.info("BorderCache: {}, MorePointTestCache: {}, Worker30SemLock: {}",
                    Worker.in_bounds.cache_info(),
                    len(spawns.have_point_cache),
                    Worker30.coroutine_semaphore.locked())

        LOOP.call_later(refresh, self.update_stats)

    def get_dots_and_messages(self):
        """Returns status dots and status messages for workers

        Dots meaning:
        . = visited more than a minute ago
        , = visited less than a minute ago, no pokemon seen
        0 = visited less than a minute ago, no pokemon or forts seen
        : = visited less than a minute ago, pokemon seen
        ! = currently visiting
        | = cleaning bag
        $ = spinning a PokéStop
        * = sending a notification
        ~ = encountering a Pokémon
        I = initial, haven't done anything yet
        » = waiting to log in (limited by SIMULTANEOUS_LOGINS)
        ° = waiting to start app simulation (limited by SIMULTANEOUS_SIMULATION)
        ∞ = bootstrapping
        L = logging in
        A = simulating app startup
        T = completing the tutorial
        X = something bad happened
        C = CAPTCHA
        G = scanning a Gym for details

        Other letters: various errors and procedures
        """
        dots = []
        messages = []
        row = []
        for i, worker in enumerate(self.workers):
            if i > 0 and i % conf.GRID[1] == 0:
                dots.append(row)
                row = []
            if worker.error_code in BAD_STATUSES:
                row.append('X')
                messages.append(worker.status.ljust(20))
            elif worker.error_code:
                row.append(worker.error_code[0])
            else:
                row.append('.')
        if row:
            dots.append(row)
        return dots, messages

    def update_coroutines_count(self, simple=True, loop=LOOP):
        try:
            tasks = Task.all_tasks(loop)
            self.coroutines_count = len(tasks) if simple else sum(not t.done() for t in tasks)
        except RuntimeError:
            # Set changed size during iteration
            self.coroutines_count = '-1'

    def _print_status(self, _ansi=ANSI, _start=datetime.now(), _notify=conf.NOTIFY or conf.NOTIFY_RAIDS):
        running_for = datetime.now() - _start

        seconds_since_start = running_for.seconds - self.idle_seconds or 0.1
        hours_since_start = seconds_since_start / 3600

        try:
            percent_skip = (self.skipped * 100) // (self.visits + self.skipped)
        except (ZeroDivisionError):
            percent_skip = '?'

        try:
            output = [
                '{}Monocle/Alternate ({}) running for {}'.format(_ansi, conf.INSTANCE_ID, running_for),
                self.counts,
                self.stats,
                self.pokemon_found,
                ('Visits: {}, per second: {:.2f}\n'
                 'Skipped: {} ({}%), unnecessary: {}').format(
                    self.visits, self.visits / seconds_since_start,
                    self.skipped,
                    percent_skip,
                    self.redundant)
            ]
        except (AttributeError):
            output = []

        try:
            seen = Worker.g['seen']
            captchas = Worker.g['captchas']
            output.append('Seen per visit: {v:.2f}, per minute: {m:.0f}'.format(
                v=seen / self.visits, m=seen / (seconds_since_start / 60)))

            if captchas:
                captchas_per_request = captchas / (self.visits / 1000)
                captchas_per_hour = captchas / hours_since_start
                output.append(
                    'CAPTCHAs per 1K visits: {r:.1f}, per hour: {h:.1f}, total: {t:d}'.format(
                    r=captchas_per_request, h=captchas_per_hour, t=captchas))
        except ZeroDivisionError:
            pass

        try:
            hash_status = HashServer.status
            output.append('Hashes: {}/{}, refresh in {:.0f}'.format(
                hash_status['remaining'],
                hash_status['maximum'],
                hash_status['period'] - time()
            ))
        except (KeyError, TypeError):
            pass

        if _notify:
            sent = Worker.notifier.sent
            output.append('Notifications sent: {}, per hour {:.1f}'.format(
                sent, sent / hours_since_start))

        output.append('')
        if not self.all_seen:
            no_sightings = ', '.join(str(w.worker_no)
                                     for w in self.workers
                                     if w.total_seen == 0)
            if no_sightings:
                output += ['Workers without sightings so far:', no_sightings, '']
            else:
                self.all_seen = True

        dots, messages = self.get_dots_and_messages()
        output += [' '.join(row) for row in dots]
        previous = 0
        for i in range(4, len(messages) + 4, 4):
            output.append('\t'.join(messages[previous:i]))
            previous = i
        if self.paused:
            output.append('\nCAPTCHAs are needed to proceed.')
        if not _ansi:
            system('cls')
        print('\n'.join(output))

    def longest_running(self):
        workers = (x for x in self.workers if x.start_time)
        worker = next(workers)
        earliest = worker.start_time
        for w in workers:
            if w.start_time < earliest:
                worker = w
                earliest = w.start_time
        minutes = ((time() * 1000) - earliest) / 60000
        return worker, minutes

    def get_start_point(self):
        smallest_diff = float('inf')
        now = time() % 3600
        closest = None

        for spawn_id, spawn_time in spawns.known.values():
            time_diff = now - spawn_time
            if 0 < time_diff < smallest_diff:
                smallest_diff = time_diff
                closest = spawn_id
            if smallest_diff < 3:
                break
        return closest

    async def update_spawns(self, initial=False):
        while True:
            try:
                await run_threaded(spawns.update)
                LOOP.create_task(run_threaded(spawns.pickle))
            except OperationalError as e:
                self.log.exception('Operational error while trying to update spawns.')
                if initial:
                    raise OperationalError('Could not update spawns, ensure your DB is set up.') from e
                await sleep(15, loop=LOOP)
            except CancelledError:
                raise
            except Exception as e:
                self.log.exception('A wild {} appeared while updating spawns!', e.__class__.__name__)
                await sleep(15, loop=LOOP)
            else:
                break

    async def launch(self, bootstrap, pickle):
        exceptions = 0
        self.next_mystery_reload = 0

        #if not pickle or not spawns.unpickle():
        await self.update_spawns(initial=True)

        FORT_CACHE.preload()
        FORT_CACHE.pickle()
        SIGHTING_CACHE.preload()
        ENCOUNTER_CACHE.preload()
        RAID_CACHE.preload()

        self.Worker30 = Worker30
        self.ENCOUNTER_CACHE = ENCOUNTER_CACHE
        self.worker30 = LOOP.create_task(Worker30.launch(overseer=self))

        self.WorkerRaider = WorkerRaider
        self.worker_raider = LOOP.create_task(WorkerRaider.launch(overseer=self))

        if not spawns or bootstrap:
            try:
                await self.bootstrap()
                await self.update_spawns()
            except CancelledError:
                return

        update_spawns = False
        self.mysteries = spawns.mystery_gen()
        while True:
            try:
                await self._launch(update_spawns)
                update_spawns = True
            except CancelledError:
                return
            except Exception:
                exceptions += 1
                if exceptions > 25:
                    self.log.exception('Over 25 errors occured in launcher loop, exiting.')
                    return False
                else:
                    self.log.exception('Error occured in launcher loop.')
                    update_spawns = False

    async def _launch(self, update_spawns):
        if update_spawns:
            await self.update_spawns()
            ACCOUNTS = get_accounts()
            LOOP.create_task(run_threaded(dump_pickle, 'accounts', ACCOUNTS))
            spawns_iter = iter(spawns.items())
        else:
            start_point = self.get_start_point()
            if start_point and not spawns.after_last():
                spawns_iter = dropwhile(
                    lambda s: s[1][0] != start_point, spawns.items())
            else:
                spawns_iter = iter(spawns.items())

        current_hour = get_current_hour()
        if spawns.after_last():
            current_hour += 3600

        captcha_limit = conf.MAX_CAPTCHAS
        skip_spawn = conf.SKIP_SPAWN
        for point, (spawn_id, spawn_seconds) in spawns_iter:
            try:
                if self.captcha_queue.qsize() > captcha_limit:
                    self.paused = True
                    self.idle_seconds += await run_threaded(self.captcha_queue.full_wait, conf.MAX_CAPTCHAS)
                    self.paused = False
            except (EOFError, BrokenPipeError, FileNotFoundError):
                pass

            spawn_time = spawn_seconds + current_hour
            spawns.spawn_timestamps[spawn_id] = spawn_time

            # negative = hasn't happened yet
            # positive = already happened
            time_diff = time() - spawn_time

            while time_diff < 0.5:
                try:
                    mystery_point = next(self.mysteries)

                    await self.coroutine_semaphore.acquire()
                    LOOP.create_task(self.try_point(mystery_point))
                except StopIteration:
                    if self.next_mystery_reload < monotonic():
                        self.mysteries = spawns.mystery_gen()
                        self.next_mystery_reload = monotonic() + conf.RESCAN_UNKNOWN
                    else:
                        await sleep(min(spawn_time - time() + .5, self.next_mystery_reload - monotonic()), loop=LOOP)
                time_diff = time() - spawn_time

            if time_diff > 5 and spawn_id in SIGHTING_CACHE.spawn_ids:
                self.redundant += 1
                continue
            elif time_diff > skip_spawn:
                self.skipped += 1
                continue

            await self.coroutine_semaphore.acquire()
            LOOP.create_task(self.try_point(point, spawn_time, spawn_id))

    async def try_again(self, point):
        async with self.coroutine_semaphore:
            worker = await self.best_worker(point, False)
            async with worker.busy:
                if await worker.visit(point):
                    self.visits += 1

    async def bootstrap(self):
        notifier = Notifier()
        try:
            self.log.warning('Starting bootstrap phase 1.')
            LOOP.create_task(notifier.scan_log_webhook('Bootstrap Status Change', 'Starting bootstrap phase 1.', '65300'))
            await self.bootstrap_one()
        except CancelledError:
            raise
        except Exception:
            self.log.exception('An exception occurred during bootstrap phase 1.')
            LOOP.create_task(notifier.scan_log_webhook('Bootstrap Status Change', 'An exception occurred during bootstrap phase 1.', '16060940'))

        try:
            self.log.warning('Starting bootstrap phase 2.')
            LOOP.create_task(notifier.scan_log_webhook('Bootstrap Status Change', 'Starting bootstrap phase 2.', '65300'))
            await self.bootstrap_two()
        except CancelledError:
            raise
        except Exception:
            self.log.exception('An exception occurred during bootstrap phase 2.')
            LOOP.create_task(notifier.scan_log_webhook('Bootstrap Status Change', 'An exception occurred during bootstrap phase 2.', '16060940'))

        self.log.warning('Starting bootstrap phase 3.')
        LOOP.create_task(notifier.scan_log_webhook('Bootstrap Status Change', 'Starting bootstrap phase 3.', '65300'))
        unknowns = list(spawns.unknown)
        shuffle(unknowns)
        tasks = (self.try_again(point) for point in unknowns)
        await gather(*tasks, loop=LOOP)
        self.log.warning('Finished bootstrapping. Updating gym parks')
        LOOP.create_task(notifier.scan_log_webhook('Bootstrap Status Change', 'Finished bootstrapping.', '65300'))
        with Parks() as parks:
            parks_thread = Thread(target=parks.reset_parks)
            parks_thread.start()
        self.bootstrapping = False

    async def bootstrap_one(self):
        async def visit_release(worker, num, *args):
            async with self.coroutine_semaphore:
                async with worker.busy:
                    point = get_start_coords(num, *args)
                    self.log.warning('start_coords: {}', point)
                    self.visits += await worker.bootstrap_visit(point)

        if bounds.multi:
            areas = [poly.polygon.area for poly in bounds.polygons]
            area_sum = sum(areas)
            percentages = [area / area_sum for area in areas]
            tasks = []
            for i, workers in enumerate(percentage_split(
                    self.workers, percentages)):
                grid = best_factors(len(workers))
                tasks.extend(visit_release(w, n, grid, bounds.polygons[i])
                             for n, w in enumerate(workers))
        else:
            tasks = (visit_release(w, n) for n, w in enumerate(self.workers))
        await gather(*tasks, loop=LOOP)

    async def bootstrap_two(self):
        async def bootstrap_try(point):
            async with self.coroutine_semaphore:
                randomized = randomize_point(point, randomization)
                LOOP.call_later(1790, LOOP.create_task, self.try_again(randomized))
                worker = await self.best_worker(point, False)
                async with worker.busy:
                    self.visits += await worker.bootstrap_visit(point)

        # randomize to within ~140m of the nearest neighbor on the second visit
        randomization = conf.BOOTSTRAP_RADIUS / 155555 - 0.00045
        tasks = (bootstrap_try(x) for x in get_bootstrap_points(bounds))
        await gather(*tasks, loop=LOOP)

    async def try_point(self, point, spawn_time=None, spawn_id=None):
        try:
            point = randomize_point(point)
            skip_time = monotonic() + (conf.GIVE_UP_KNOWN if spawn_time else conf.GIVE_UP_UNKNOWN)
            worker = await self.best_worker(point, skip_time)
            if not worker:
                if spawn_time:
                    self.skipped += 1
                return
            async with worker.busy:
                if spawn_time:
                    worker.after_spawn = time() - spawn_time

                if await worker.visit(point, spawn_id):
                    self.visits += 1
        except CancelledError:
            raise
        except Exception:
            self.log.exception('An exception occurred in try_point')
        finally:
            self.coroutine_semaphore.release()

    async def best_worker(self, point, skip_time):
        good_enough = conf.GOOD_ENOUGH
        while self.running:
            gen = (w for w in self.workers if not w.busy.locked())
            try:
                worker = next(gen)
                lowest_speed = worker.travel_speed(point)
            except StopIteration:
                lowest_speed = float('inf')
            for w in gen:
                speed = w.travel_speed(point)
                if speed < lowest_speed:
                    lowest_speed = speed
                    worker = w
                    if speed < good_enough:
                        break
            if lowest_speed < conf.SPEED_LIMIT:
                worker.speed = lowest_speed
                return worker
            if skip_time and monotonic() > skip_time:
                return None
            await sleep(conf.SEARCH_SLEEP, loop=LOOP)

    def refresh_dict(self):
        ACCOUNTS = get_accounts()
        while not self.extra_queue.empty():
            account = self.extra_queue.get()
            username = account['username']
            ACCOUNTS[username] = account
