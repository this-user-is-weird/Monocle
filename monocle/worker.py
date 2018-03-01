import traceback

from s2sphere import CellId, LatLng
from asyncio import gather, Lock, Semaphore, sleep, CancelledError
from collections import deque
from time import time, monotonic
from queue import Empty, Full
from itertools import cycle
from sys import exit
from math import ceil
from distutils.version import StrictVersion
from functools import lru_cache

from aiohttp import ClientSession
from aiopogo import PGoApi, HashServer, json_loads, exceptions as ex
from aiopogo.auth_ptc import AuthPtc
from cyrandom import choice, randint, uniform
from pogeo import get_distance

from .db import FORT_CACHE, MYSTERY_CACHE, SIGHTING_CACHE, RAID_CACHE
from .utils import round_coords, load_pickle, get_device_info, get_start_coords, Units, randomize_point, calc_pokemon_level
from .shared import get_logger, LOOP, SessionManager, run_threaded, TtlCache
from .sb import SbDetector, SbAccountException
from .accounts import Account, get_accounts, InsufficientAccountsException, LoginCredentialsException, \
        EmailUnverifiedException, SecurityLockException, TempDisabledException
from . import altitudes, avatar, bounds, db_proc, spawns, sanitized as conf
from .notification import Notifier
from .weather import WEATHER_CACHE, Weather

from python_anticaptcha import AnticaptchaClient, NoCaptchaTaskProxylessTask
from python_anticaptcha.exceptions import AnticatpchaException

if conf.CACHE_CELLS:
    from array import typecodes
    if 'Q' in typecodes:
        from pogeo import get_cell_ids_compact as _pogeo_cell_ids
    else:
        from pogeo import get_cell_ids as _pogeo_cell_ids
else:
    from pogeo import get_cell_ids as _pogeo_cell_ids

if conf.SB_DETECTOR:
    sb_detector = SbDetector()
else:
    sb_detector = None

_unit = getattr(Units, conf.SPEED_UNIT.lower())
if conf.SPIN_POKESTOPS:
    if _unit is Units.miles:
        SPINNING_SPEED_LIMIT = 21
        UNIT_STRING = "MPH"
    elif _unit is Units.kilometers:
        SPINNING_SPEED_LIMIT = 34
        UNIT_STRING = "KMH"
    elif _unit is Units.meters:
        SPINNING_SPEED_LIMIT = 34000
        UNIT_STRING = "m/h"
UNIT = _unit.value
del _unit


class Worker:
    """Single worker walking on the map"""

    download_hash = ''
    scan_delay = conf.SCAN_DELAY if conf.SCAN_DELAY >= 10 else 10
    g = {'seen': 0, 'captchas': 0}
    more_point_cell_cache = TtlCache(ttl=300)
    has_raiders = (conf.RAIDERS_PER_GYM and conf.RAIDERS_PER_GYM > 0)
    scan_gym = conf.GYM_NAMES or conf.GYM_DEFENDERS
    passive_scan_gym = (scan_gym and not has_raiders)
    log = get_logger("worker")

    if conf.CACHE_CELLS:
        cells = load_pickle('cells') or {}

        @classmethod
        def get_cell_ids(cls, point):
            rounded = round_coords(point, 4)
            try:
                return cls.cells[rounded]
            except KeyError:
                cells = _pogeo_cell_ids(rounded)
                cls.cells[rounded] = cells
                return cells
    else:
        get_cell_ids = _pogeo_cell_ids

    login_semaphore = Semaphore(conf.SIMULTANEOUS_LOGINS, loop=LOOP)
    sim_semaphore = Semaphore(conf.SIMULTANEOUS_SIMULATION, loop=LOOP)
    update_account_lock = Lock(loop=LOOP)

    multiproxy = False
    if conf.PROXIES:
        if len(conf.PROXIES) > 1:
            multiproxy = True
        proxies = cycle(conf.PROXIES)
    else:
        proxies = None

    notifier = Notifier()

    if conf.PGSCOUT_ENDPOINT:
	    PGScout_cycle=cycle(conf.PGSCOUT_ENDPOINT)

    def __init__(self, worker_no, overseer, captcha_queue, account_queue, worker_dict, account_dict, start_coords=None):
        name = self.__class__.__name__.lower()
        self.worker_no = worker_no
        self.overseer = overseer
        self.log = get_logger('{}-{}'.format(name, worker_no))
        self.worker_no = worker_no
        self.account_queue = account_queue
        self.captcha_queue = captcha_queue
        self.worker_dict = worker_dict
        self.account_dict = account_dict

        # account information
        try:
            self.account = self.account_queue.get_nowait()
        except (Empty, InsufficientAccountsException) as e:
            try:
                self.account = self.captcha_queue.get_nowait()
            except Empty as e:
                self.account = None
                self.username = None
                #raise InsufficientAccountsException("You don't have enough accounts for the number of workers specified in GRID.") from e
        self.altitude = None
        try:
            self.location = self.account['location'][:2]
        except Exception as e:
            if start_coords:
                self.location = start_coords
            else:
                self.location = self.get_start_coords()
        if self.account:
            self.username = self.account['username']
            # last time of any request
            self.last_request = self.account.get('time', 0)
            try:
                self.items = self.account['items']
                self.bag_items = sum(self.items.values())
            except KeyError:
                self.account['items'] = {}
                self.items = self.account['items']
            self.inventory_timestamp = self.account.get('inventory_timestamp', 0) if self.items else 0
            self.player_level = self.account.get('level', 0)
            self.initialize_api()
        else:
            self.last_request = 0
            self.items = {}
            self.bag_items = 0
            self.inventory_timestamp = 0
            self.player_level = 0
        # last time of a request that requires user interaction in the game
        self.last_action = self.last_request
        # last time of a GetMapObjects request
        self.last_gmo = self.last_request
        self.num_captchas = 0
        self.eggs = {}
        self.unused_incubators = deque()
        # State variables
        self.busy = Lock(loop=LOOP)
        # Other variables
        self.after_spawn = 0
        self.speed = 0
        self.total_seen = 0
        self.error_code = 'INIT'
        self.item_capacity = 350
        self.visits = 0
        self.pokestops = conf.SPIN_POKESTOPS
        self.next_spin = 0
        self.handle = HandleStub()
        self.next_encounter_delay = 0.25
        self.last_encounter = int(round(time() * 1000))

    def needs_sleep(self):
        return True

    def min_level(self):
        return 0

    def max_level(self):
        return 29

    def get_start_coords(self):
        return get_start_coords(self.worker_no)

    def estimated_extra_accounts(self):
        return Account.estimated_extra_accounts()

    def required_extra_accounts(self):
        return int(ceil(conf.EXTRA_ACCOUNT_PERCENT * conf.GRID[0] * conf.GRID[1]))

    def initialize_api(self):
        device_info = get_device_info(self.account)
        self.empty_visits = 0

        self.api = PGoApi(device_info=device_info)
        self.api.set_position(*self.location, self.altitude)
        if self.proxies:
            self.api.proxy = next(self.proxies)
        try:
            if self.account['provider'] == 'ptc' and 'auth' in self.account:
                self.api.auth_provider = AuthPtc(username=self.username, password=self.account['password'], timeout=conf.LOGIN_TIMEOUT)
                self.api.auth_provider._access_token = self.account['auth']
                self.api.auth_provider._access_token_expiry = self.account['expiry']
                if self.api.auth_provider.check_access_token():
                    self.api.auth_provider.authenticated = True
        except KeyError:
            pass

    @lru_cache(maxsize=1048576)
    def in_bounds(self, lat, lon):
        return (lat, lon) in bounds

    def swap_proxy(self):
        proxy = self.api.proxy
        self.api.proxy = next(self.proxies)

    async def login(self, reauth=False):
        """Logs worker in and prepares for scanning"""
        self.log.info('Trying to log in {}', self.username)

        for attempt in range(-1, conf.MAX_RETRIES):
            if not self.overseer.running:
                raise OverseerNotRunningException("During login attempt.")
            try:
                self.error_code = '»'
                async with self.login_semaphore:
                    self.error_code = 'LOGIN'
                    await self.api.set_authentication(
                        username=self.username,
                        password=self.account['password'],
                        provider=self.account.get('provider') or 'ptc',
                        timeout=conf.LOGIN_TIMEOUT
                    )
            except ex.UnexpectedAuthError as e:
                await self.swap_account('unexpected auth error')
            except ex.AuthException as e:
                msg = str(e)
                if ("Your username or password is incorrect" in msg):
                    if attempt >= min(1,conf.MAX_RETRIES - 1):
                        raise LoginCredentialsException("Username or password is wrong.")
                elif "email not verified" in msg:
                    raise EmailUnverifiedException("Account email not verified")
                elif "your account has been disabled for 15 minutes" in msg:
                    raise TempDisabledException("Account disabled for 15 mins due to multiple failed logins")
                elif "has been locked for security reasons" in msg:
                    raise SecurityLockException("Account locked for security reason. Reset password needed")
                err = e
                await sleep(2, loop=LOOP)
            else:
                err = None
                break
        if reauth:
            if err:
                self.error_code = 'NOT AUTHENTICATED'
                self.log.info('Re-auth error on {}: {}', self.username, err)
                return False
            self.error_code = None
            return True
        if err:
            raise err

        self.error_code = '°'
        version = 9100
        async with self.sim_semaphore:
            self.error_code = 'APP SIMULATION'
            if conf.APP_SIMULATION:
                await self.app_simulation_login(version)
            else:
                await self.download_remote_config(version)

        self.error_code = None
        return True

    async def get_player(self):
        request = self.api.create_request()
        request.get_player(player_locale=conf.PLAYER_LOCALE)

        responses = await self.call(request, settings=False, chain=False)

        tutorial_state = None
        try:
            get_player = responses['GET_PLAYER']

            if get_player.warn:
                if conf.ACCOUNTS_SWAP_OUT_ON_WARN:
                    raise ex.WarnAccountException
                else:
                    self.log.warning('{} is warn but not swapped out due to ACCOUNTS_SWAP_OUT_ON_WARN being False.', self.username)
            if get_player.banned:
                raise ex.BannedAccountException

            player_data = get_player.player_data
            tutorial_state = player_data.tutorial_state

            # API can return 0 as capacity.
            if player_data.max_item_storage != 0:
                self.item_capacity = player_data.max_item_storage

            if 'created' not in self.account:
                self.account['created'] = player_data.creation_timestamp_ms / 1000
        except (KeyError, TypeError, AttributeError):
            pass
        return tutorial_state

    async def download_remote_config(self, version):
        request = self.api.create_request()
        request.download_remote_config_version(platform=1, device_model=self.account['model'], app_version=version)
        responses = await self.call(request, buddy=False, inbox=False, dl_hash=False)

        try:
            inventory_items = responses['GET_INVENTORY'].inventory_delta.inventory_items
            for item in inventory_items:
                level = item.inventory_item_data.player_stats.level
                if level:
                    self.player_level = level
                    break
        except KeyError:
            pass

        await self.random_sleep(.78, 1.05)
        try:
            remote_config = responses['DOWNLOAD_REMOTE_CONFIG_VERSION']
            return (
                remote_config.asset_digest_timestamp_ms / 1000000,
                remote_config.item_templates_timestamp_ms / 1000)
        except KeyError:
            return 0.0, 0.0

    async def set_avatar(self, tutorial=False):
        plater_avatar = avatar.new()
        request = self.api.create_request()
        request.list_avatar_customizations(
            avatar_type=plater_avatar['avatar'],
            slot=tuple(),
            filters=(2,)
        )
        await self.call(request, buddy=not tutorial, inbox=False, action=5)
        await self.random_sleep(7, 14)

        request = self.api.create_request()
        request.set_avatar(player_avatar=plater_avatar)
        await self.call(request, buddy=not tutorial, inbox=False, action=2)

        if tutorial:
            await self.random_sleep(.5, 4)

            request = self.api.create_request()
            request.mark_tutorial_complete(tutorials_completed=(1,))
            await self.call(request, buddy=False, inbox=False)

        await self.random_sleep(.5, 1)

        request = self.api.create_request()
        request.get_player_profile()
        await self.call(request, inbox=False, action=1)

    async def app_simulation_login(self, version):
        self.log.info('{} is starting RPC login sequence (iOS app simulation)', self.username)

        # empty request
        # request = self.api.create_request()
        # await self.call(request, settings=False, chain=False)
        # await self.random_sleep(.43, .97)

        # request 1: get_player
        tutorial_state = await self.get_player()
        await self.random_sleep(.53, 1.1)

        # request 2: download_remote_config_version
        asset_time, template_time = await self.download_remote_config(version)

        if asset_time > self.account.get('asset_time', 0.0):
            # request 3: get_asset_digest
            i = randint(0, 3)
            result = 2
            page_offset = 0
            page_timestamp = 0
            while result == 2:
                request = self.api.create_request()
                request.get_asset_digest(
                    platform=1,
                    app_version=version,
                    paginate=True,
                    page_offset=page_offset,
                    page_timestamp=page_timestamp)
                responses = await self.call(request, buddy=False, inbox=False)
                if i > 2:
                    await sleep(1.45)
                    i = 0
                else:
                    i += 1
                    await sleep(.2)
                try:
                    response = responses['GET_ASSET_DIGEST']
                except KeyError:
                    break
                result = response.result
                page_offset = response.page_offset
                page_timestamp = response.timestamp_ms
            self.account['asset_time'] = asset_time

        if template_time > self.account.get('template_time', 0.0):
            # request 4: download_item_templates
            i = randint(0, 3)
            result = 2
            page_offset = 0
            page_timestamp = 0
            while result == 2:
                request = self.api.create_request()
                request.download_item_templates(
                    paginate=True,
                    page_offset=page_offset,
                    page_timestamp=page_timestamp)
                responses = await self.call(request, buddy=False, inbox=False)
                if i > 2:
                    await sleep(1.5)
                    i = 0
                else:
                    i += 1
                    await sleep(.25)
                try:
                    response = responses['DOWNLOAD_ITEM_TEMPLATES']
                except KeyError:
                    break
                result = response.result
                page_offset = response.page_offset
                page_timestamp = response.timestamp_ms

            self.account['template_time'] = template_time

        if (conf.COMPLETE_TUTORIAL and
                tutorial_state is not None and
                not all(x in tutorial_state for x in (0, 1, 3, 4, 7))):
            self.log.warning('{} is starting tutorial', self.username)
            await self.complete_tutorial(tutorial_state)
        else:
            # request 5: get_player_profile
            request = self.api.create_request()
            request.get_player_profile()
            await self.call(request, inbox=False)
            await self.random_sleep(.2, .3)

            if self.player_level:
                # request 6: level_up_rewards
                request = self.api.create_request()
                request.level_up_rewards(level=self.player_level)
                await self.call(request)
                await self.random_sleep(.45, .7)
            else:
                self.log.warning('No player level')

            request = self.api.create_request()
            request.get_store_items()
            await self.call(request, settings=False, chain=False)
            await self.random_sleep(.43, .97)

            self.log.info('{} finished RPC login sequence (iOS app simulation)', self.username)
            await self.random_sleep(.5, 1.3)
        self.error_code = None
        return True

    async def complete_tutorial(self, tutorial_state):
        self.error_code = 'TUTORIAL'
        if 0 not in tutorial_state:
            # legal screen
            request = self.api.create_request()
            request.mark_tutorial_complete(tutorials_completed=(0,))
            await self.call(request, buddy=False, inbox=False)

            await self.random_sleep(.35, .525)

            request = self.api.create_request()
            request.get_player(player_locale=conf.PLAYER_LOCALE)
            await self.call(request, buddy=False, inbox=False)
            await sleep(1)

        if 1 not in tutorial_state:
            # avatar selection
            await self.set_avatar(tutorial=True)

        starter_id = None
        if 3 not in tutorial_state:
            # encounter tutorial
            await self.random_sleep(.7, .9)
            request = self.api.create_request()
            request.get_download_urls(asset_id=
                ('1a3c2816-65fa-4b97-90eb-0b301c064b7a/1487275569649000',
                'aa8f7687-a022-4773-b900-3a8c170e9aea/1487275581132582',
                'e89109b0-9a54-40fe-8431-12f7826c8194/1487275593635524'))
            await self.call(request, inbox=False)

            await self.random_sleep(7, 10.3)
            request = self.api.create_request()
            starter = choice((1, 4, 7))
            request.encounter_tutorial_complete(pokemon_id=starter)
            responses = await self.call(request, inbox=False, action=1)

            try:
                inventory = responses['GET_INVENTORY'].inventory_delta.inventory_items
                for item in inventory:
                    pokemon = item.inventory_item_data.pokemon_data
                    if pokemon.id:
                        starter_id = pokemon.id
                        break
            except (KeyError, TypeError):
                starter_id = None

            await self.random_sleep(.4, .5)
            request = self.api.create_request()
            request.get_player(player_locale=conf.PLAYER_LOCALE)
            await self.call(request, inbox=False)

        if 4 not in tutorial_state:
            # name selection
            await self.random_sleep(12, 18)
            request = self.api.create_request()
            request.claim_codename(codename=self.username)
            await self.call(request, inbox=False, action=2)

            await sleep(.7, loop=LOOP)
            request = self.api.create_request()
            request.get_player(player_locale=conf.PLAYER_LOCALE)
            await self.call(request, inbox=False)
            await sleep(.13, loop=LOOP)

            request = self.api.create_request()
            request.mark_tutorial_complete(tutorials_completed=(4,))
            await self.call(request, inbox=False)

        if 7 not in tutorial_state:
            # first time experience
            await self.random_sleep(3.9, 4.5)
            request = self.api.create_request()
            request.mark_tutorial_complete(tutorials_completed=(7,))
            await self.call(request, inbox=False)

        if starter_id:
            await self.random_sleep(4, 5)
            request = self.api.create_request()
            request.set_buddy_pokemon(pokemon_id=starter_id)
            await self.call(request, inbox=False, action=2)
            await self.random_sleep(.8, 1.2)

        await sleep(.2, loop=LOOP)
        return True

    def update_inventory(self, inventory_items):
        for thing in inventory_items:
            obj = thing.inventory_item_data
            if obj.HasField('item'):
                item = obj.item
                self.items[item.item_id] = item.count
                self.bag_items = sum(self.items.values())
            elif conf.INCUBATE_EGGS:
                if obj.HasField('pokemon_data') and obj.pokemon_data.is_egg:
                    egg = obj.pokemon_data
                    self.eggs[egg.id] = egg
                elif obj.HasField('egg_incubators'):
                    self.unused_incubators.clear()
                    for item in obj.egg_incubators.egg_incubator:
                        if item.pokemon_id:
                            continue
                        if item.item_id == 901:
                            self.unused_incubators.append(item)
                        else:
                            self.unused_incubators.appendleft(item)

    async def call(self, request, chain=True, buddy=True, settings=True, inbox=True, dl_hash=True, action=None):
        if chain:
            request.check_challenge()
            request.get_hatched_eggs()
            request.get_inventory(last_timestamp_ms=self.inventory_timestamp)
            request.check_awarded_badges()
            if settings:
                if dl_hash:
                    request.download_settings(hash=self.download_hash)
                else:
                    request.download_settings()
            if buddy:
                request.get_buddy_walked()
            if inbox:
                request.get_inbox(is_history=True)

        if action and (self.needs_sleep()):
            now = time()
            # wait for the time required, or at least a half-second
            if self.last_action > now + .5:
                await sleep(self.last_action - now, loop=LOOP)
            else:
                await sleep(0.5, loop=LOOP)

        response = None
        err = None
        for attempt in range(-1, conf.MAX_RETRIES):
            try:
                if not self.overseer.running:
                    raise OverseerNotRunningException("During RPC call attempt.")
                responses = await request.call()
                self.last_request = time()
                err = None
                break
            except (ex.NotLoggedInException, ex.AuthException) as e:
                self.log.info('Auth error on {} in call: {}', self.username, e)
                err = e
                await sleep(3, loop=LOOP)
                if not await self.login(reauth=True):
                    await self.swap_account(reason='reauth failed')
            except ex.TimeoutException as e:
                self.error_code = 'TIMEOUT'
                if not isinstance(e, type(err)):
                    err = e
                    self.log.warning('{}', e)
                await sleep(10, loop=LOOP)
            except ex.HashingOfflineException as e:
                if not isinstance(e, type(err)):
                    err = e
                    self.log.warning('{}', e)
                self.error_code = 'HASHING OFFLINE'
                await sleep(5, loop=LOOP)
            except ex.NianticOfflineException as e:
                if not isinstance(e, type(err)):
                    err = e
                    self.log.warning('{}', e)
                self.error_code = 'NIANTIC OFFLINE'
                await self.random_sleep()
            except ex.HashingQuotaExceededException as e:
                if not isinstance(e, type(err)):
                    err = e
                    self.log.warning('Exceeded your hashing quota, sleeping.')
                self.error_code = 'QUOTA EXCEEDED'
                refresh = HashServer.status.get('period')
                now = time()
                if refresh:
                    if refresh > now:
                        await sleep(refresh - now + 1, loop=LOOP)
                    else:
                        await sleep(5, loop=LOOP)
                else:
                    await sleep(30, loop=LOOP)
            except ex.BadRPCException:
                raise
            except ex.InvalidRPCException as e:
                self.last_request = time()
                if not isinstance(e, type(err)):
                    err = e
                    self.log.warning('{}', e)
                self.error_code = 'INVALID REQUEST'
                await self.random_sleep(0.9, 2.0)
            except ex.ProxyException as e:
                if not isinstance(e, type(err)):
                    err = e
                self.error_code = 'PROXY ERROR'

                if self.multiproxy:
                    self.log.error('{}, swapping proxy.', e)
                    self.swap_proxy()
                else:
                    if not isinstance(e, type(err)):
                        self.log.error('{}', e)
                    await sleep(5, loop=LOOP)
            except (ex.MalformedResponseException, ex.UnexpectedResponseException) as e:
                self.last_request = time()
                if not isinstance(e, type(err)):
                    self.log.warning('{}', e)
                self.error_code = 'MALFORMED RESPONSE'
                await self.random_sleep()
        if err is not None:
            raise err

        if action:
            # pad for time that action would require
            self.last_action = self.last_request + action

        try:
            delta = responses['GET_INVENTORY'].inventory_delta
            self.inventory_timestamp = delta.new_timestamp_ms
            self.update_inventory(delta.inventory_items)
        except KeyError:
            pass

        if settings:
            try:
                dl_settings = responses['DOWNLOAD_SETTINGS']
                Worker.download_hash = dl_settings.hash
            except KeyError:
                self.log.info('Missing DOWNLOAD_SETTINGS response.')
            else:
                if (not dl_hash
                        and conf.FORCED_KILL
                        and dl_settings.settings.minimum_client_version != '0.91.2'):
                    forced_version = StrictVersion(dl_settings.settings.minimum_client_version)
                    if forced_version > StrictVersion('0.91.2'):
                        err = '{} is being forced, exiting.'.format(forced_version)
                        self.log.error(err)
                        print(err)
                        exit()
        try:
            challenge_url = responses['CHECK_CHALLENGE'].challenge_url
            if challenge_url != ' ':
                self.g['captchas'] += 1
                if conf.CAPTCHA_KEY:
                    self.log.warning('{} has encountered a CAPTCHA, trying to solve', self.username)
                    await self.handle_captcha(challenge_url)
                else:
                    raise CaptchaException
        except KeyError:
            pass
        return responses

    def travel_speed(self, point):
        '''Fast calculation of travel speed to point'''
        time_diff = max(time() - self.last_request, self.scan_delay)
        distance = get_distance(self.location, point, UNIT)
        # conversion from seconds to hours
        speed = (distance / time_diff) * 3600
        return speed

    async def sleep_travel_time(self, point, max_speed=conf.SPEED_LIMIT):
        distance = get_distance(self.location, point, UNIT)
        time_needed = 3600.0 * distance / max_speed
        if self.username:
            if time_needed > 0.0:
                self.log.info("{} needs {}s of travel time.", self.username, int(time_needed))
                await sleep(time_needed, loop=LOOP)
                return True
        return False

    async def account_promotion(self):
        if self.player_level and self.player_level >= 30:
            self.log.warning('Congratulations {} has reached Lv.30. Moving it out of low-level slave pool', self.username)
            await sleep(1, loop=LOOP)
            await self.remove_account(flag='level30')

    async def bootstrap_visit(self, point):
        for _ in range(3):
            if await self.visit(point, bootstrap=True):
                return True
            self.error_code = '8'
            self.simulate_jitter(0.00005)
        return False

    async def visit(self, point, spawn_id=None, bootstrap=False,
            encounter_id=None, encounter_only=False, sighting=None,
            visiting_pokestop=False, gym=None):
        """Wrapper for self.visit_point - runs it a few times before giving up

        Also is capable of restarting in case an error occurs.
        """
        while self.overseer.running and not self.account:
            self.error_code = 'D'
            self.log.warning("No account being set for visit. Probably due to insufficient accounts. Retrying in 30s.")
            await self.random_sleep(10.0, 60.0)
            if Account.estimated_extra_accounts() > 0:
                await self.new_account()

        # Intended to quit if account is still None at this stage
        if self.account is None:
            return

        try:
            await self.account_promotion()

            if sb_detector:
                await sb_detector.detect(self.account)

            if not visiting_pokestop and self.player_level is not None and self.player_level <= 1:
                await self.visit_nearest_pokestop(point)

            try:
                self.altitude = altitudes.get(point)
            except KeyError:
                self.altitude = await altitudes.fetch(point)
            self.location = point
            self.api.set_position(*self.location, self.altitude)
            if not self.authenticated:
                await self.login()
            if encounter_only and sighting:
                return await self.visit_encounter(point, sighting)
            else:
                return await self.visit_point(point, spawn_id, bootstrap,
                        encounter_id=encounter_id, gym=gym)
        except ex.NotLoggedInException:
            self.error_code = 'NOT AUTHENTICATED'
            await sleep(1, loop=LOOP)
            if not await self.login(reauth=True):
                await self.swap_account(reason='reauth failed')
            return await self.visit(point, spawn_id, bootstrap,
                    encounter_id=encounter_id,
                    encounter_only=encounter_only,
                    sighting=sighting,
                    gym=gym)
        except ex.AuthException as e:
            self.log.warning('Auth error on {} in visit: {}', self.username, e)
            self.error_code = 'NOT AUTHENTICATED'
            await sleep(3, loop=LOOP)
            await self.swap_account(reason='login failed')
        except LoginCredentialsException as e:
            self.log.warning('Login credentials error on {}: {}', self.username, e)
            self.error_code = 'WRONG CREDENTIALS'
            await sleep(3, loop=LOOP)
            await self.remove_account(flag='credentials')
        except SecurityLockException as e:
            self.log.warning('Security lock error on {}: {}', self.username, e)
            self.error_code = 'SECURITY LOCK'
            await sleep(3, loop=LOOP)
            await self.remove_account(flag='security')
        except TempDisabledException as e:
            self.log.warning('Temp disabled error on {}: {}', self.username, e)
            self.error_code = 'TEMP DISABLED'
            await sleep(3, loop=LOOP)
            await self.remove_account(flag='tempdisabled')
        except EmailUnverifiedException as e:
            self.log.warning('Email verification error on {}: {}', self.username, e)
            self.error_code = 'UNVERIFIED'
            await sleep(3, loop=LOOP)
            await self.remove_account(flag='unverified')
        except InsufficientAccountsException as e:
            await self.update_accounts_dict()
            raise InsufficientAccountsException("No more accounts to pull from DB.") from e
        except CaptchaException:
            self.error_code = 'CAPTCHA'
            self.g['captchas'] += 1
            await sleep(1, loop=LOOP)
            await self.bench_account()
        except CaptchaSolveException:
            self.error_code = 'CAPTCHA'
            await sleep(1, loop=LOOP)
            await self.swap_account(reason='solving CAPTCHA failed')
        except ex.TempHashingBanException:
            self.error_code = 'HASHING BAN'
            self.log.error('Temporarily banned from hashing server for using invalid keys.')
            await sleep(185, loop=LOOP)
        except ex.BannedAccountException:
            self.error_code = 'BANNED'
            await sleep(1, loop=LOOP)
            await self.remove_account(flag='banned')
        except SbAccountException:
            self.error_code = 'BANNED'
            await sleep(1, loop=LOOP)
            await self.remove_account(flag='sbanned')
        except ex.ProxyException as e:
            self.error_code = 'PROXY ERROR'

            if self.multiproxy:
                self.log.error('{} Swapping proxy.', e)
                self.swap_proxy()
            else:
                self.log.error('{}', e)
        except ex.TimeoutException as e:
            self.log.warning('{} Giving up.', e)
        except ex.NianticIPBannedException:
            self.error_code = 'IP BANNED'

            if self.multiproxy:
                self.log.warning('Swapping out {} due to IP ban.', self.api.proxy)
                self.swap_proxy()
            else:
                self.log.error('IP banned.')
        except ex.NianticOfflineException as e:
            await self.swap_account(reason='Niantic endpoint failure')
            self.log.warning('{}. Giving up.', e)
        except ex.ServerBusyOrOfflineException as e:
            self.log.warning('{} Giving up.', e)
        except (ex.BadRPCException, ex.WarnAccountException) as e:
            if isinstance(e, ex.BadRPCException):
                self.error_code = '3'
                flag = 'code3'
            else:
                self.error_code = 'WARN'
                flag = 'warn'
            await sleep(1, loop=LOOP)
            key = "{}_count".format(flag)
            flag_count = (self.account.get(key) or 0)
            flag_count += 1
            self.account[key] = flag_count
            self.log.warning("{} received {} {}(s).", self.username, flag_count, flag)
            if flag_count >= 3:
                await self.remove_account(flag=flag)
            else:
                await self.swap_account(reason="of {} {}(s)".format(flag_count, flag))
        except ex.InvalidRPCException as e:
            self.log.warning('{} Giving up.', e)
        except ex.ExpiredHashKeyException as e:
            self.error_code = 'KEY EXPIRED'
            err = str(e)
            self.log.error(err)
            print(err)
            time.sleep(5)
        except (ex.MalformedResponseException, ex.UnexpectedResponseException) as e:
            self.log.warning('{} Giving up.', e)
            self.error_code = 'MALFORMED RESPONSE'
        except EmptyGMOException as e:
            self.error_code = '0'
            self.log.warning('Empty GetMapObjects response for {}. Speed: {:.2f}', self.username, self.speed)
        except ex.HashServerException as e:
            self.log.warning('{}', e)
            self.error_code = 'HASHING ERROR'
        except ex.AiopogoError as e:
            self.log.exception(e.__class__.__name__)
            self.error_code = 'AIOPOGO ERROR'
        except CancelledError:
            self.log.warning('Visit cancelled.')
        except OverseerNotRunningException as e:
            self.log.warning('Overseer stopped: {}', e)
        except Exception as e:
            self.log.exception('A wild {} appeared!', e.__class__.__name__)
            self.error_code = 'EXCEPTION'
        return False

    async def visit_point(self, point, spawn_id, bootstrap,
            encounter_conf=conf.ENCOUNTER, notify_conf=conf.NOTIFY,
            more_points=conf.MORE_POINTS, encounter_id=None, gym=None):
        self.handle.cancel()
        gmo_success = False
        self.error_code = '8' if bootstrap else '!'

        self.log.info('{0} is visiting {1[0]:.4f}, {1[1]:.4f}', self.username, point)
        start = time()

        if sb_detector:
            sb_detector.add_visit(self.account)

        cell_ids = self.get_cell_ids(point)
        since_timestamp_ms = (0,) * len(cell_ids)
        request = self.api.create_request()
        request.get_map_objects(cell_id=cell_ids,
                                since_timestamp_ms=since_timestamp_ms,
                                latitude=point[0],
                                longitude=point[1])
        diff = self.last_gmo + self.scan_delay - time()
        if diff > 0:
            await sleep(diff, loop=LOOP)
        responses = await self.call(request)
        self.last_gmo = self.last_request

        try:
            map_objects = responses['GET_MAP_OBJECTS']

            if map_objects.status != 1:
                error = 'GetMapObjects code for {}. Speed: {:.2f}'.format(self.username, self.speed)
                self.empty_visits += 1
                if self.empty_visits > 3:
                    reason = '{} empty visits'.format(self.empty_visits)
                    await self.swap_account(reason)
                raise ex.UnexpectedResponseException(error)
        except KeyError:
            await self.random_sleep(.5, 1)
            await self.get_player()
            raise ex.UnexpectedResponseException('Missing GetMapObjects response.')

        pokemon_seen = 0
        forts_seen = 0
        points_seen = 0
        seen_target = not spawn_id
        seen_encounter = not encounter_id
        seen_gym = not gym
        gmo_success = True
        weather_condition = 0

        scan_gym_external_id = gym.get('external_id') if gym else None

        if conf.ITEM_LIMITS and self.bag_items >= self.item_capacity:
            await self.clean_bag()


        if map_objects.client_weather:
            for w in map_objects.client_weather:
                weather = Weather.normalize_weather(w, map_objects.time_of_day)
                weather_condition = weather['condition']
                if weather not in WEATHER_CACHE:
                    if conf.NOTIFY_WEATHER and Weather.has_weather_changed(weather):
                        LOOP.create_task(self.notifier.webhook_weather(weather))
                    db_proc.add(weather)

        for map_cell in map_objects.map_cells:
            request_time_ms = map_cell.current_timestamp_ms
            cell_weather_id = CellId(map_cell.s2_cell_id).parent(10).id()
            for pokemon in map_cell.wild_pokemons:
                pokemon_seen += 1
                if not self.in_bounds(pokemon.latitude, pokemon.longitude):
                    continue

                normalized = self.normalize_pokemon(pokemon, username=self.username)
                normalized['time_of_day'] = map_objects.time_of_day
                normalized['weather_cell_id'] = cell_weather_id
                normalized['weather'] = weather_condition
                seen_target = seen_target or normalized['spawn_id'] == spawn_id
                seen_encounter = seen_encounter or normalized['encounter_id'] == encounter_id

                # This line does not only checks the cache, it also updates the mystery seen time.
                # Tricky.
                in_mystery_cache = normalized in MYSTERY_CACHE

                if sb_detector:
                    sb_detector.add_sighting(self.account, normalized)

                # Check if already marked for save as mystery
                if normalized['type'] == 'mystery':
                    if in_mystery_cache:
                        continue
                    else:
                        MYSTERY_CACHE.add(normalized)

                if not encounter_id:
                    if self.should_skip_sighting(normalized, SIGHTING_CACHE):
                        continue

                # Check against insert list
                sp_discovered = (normalized.get('despawn') is not None)
                is_in_insert_blacklist = (conf.NO_DB_INSERT_IDS is not None and
                        normalized['pokemon_id'] in conf.NO_DB_INSERT_IDS)
                skip_insert = (sp_discovered and is_in_insert_blacklist)

                self.log.debug('Pokemon: {}, sp: {}, sp_discovered: {}, in_blacklist: {}, skip_insert: {}',
                        normalized['pokemon_id'], spawn_id, sp_discovered, is_in_insert_blacklist, skip_insert)

                # Do not insert to db for this pokemon
                if skip_insert:
                    db_proc.count += 1
                    continue

                should_delegate_encounter = (conf.LV30_PERCENT_OF_WORKERS > 0.0 and
                        (not self.player_level or self.player_level < 30))
                should_notify = self.should_notify(normalized)
                should_encounter = (sp_discovered and self.should_encounter(normalized, should_notify=should_notify))

                if encounter_id:
                    cache = self.overseer.ENCOUNTER_CACHE if should_encounter else SIGHTING_CACHE
                    if self.should_skip_sighting(normalized, cache):
                        continue

                encountered = False

                if (self.overseer.running and should_encounter):
                    if should_delegate_encounter:
                        if (normalized not in self.overseer.ENCOUNTER_CACHE and
                                'expire_timestamp' in normalized and
                                normalized['expire_timestamp']):
                            try:
                                self.overseer.Worker30.add_job(normalized)
                                self.log.debug("{} added encounter job {}, {}, ({}, {})",
                                        self.username,
                                        normalized['pokemon_id'],
                                        normalized['encounter_id'],
                                        normalized['lat'],
                                        normalized['lon'])
                                continue
                            except Full:
                                self.overseer.ENCOUNTER_CACHE.add(normalized)
                                self.log.warning('Encounter job queue is full. Skipping encounter delegation for {}', normalized['encounter_id'])
                            except Exception as e:
                                self.overseer.ENCOUNTER_CACHE.add(normalized)
                                self.log.warning('Unexpected error while adding encounter job: {}', e)
                                traceback.print_exc()
                        else:
                            continue
                    elif not encountered and self.player_level >= 30:
                        normalized['check_duplicate'] = True
                        try:
                            if await self.encounter(normalized, pokemon.spawn_point_id):
                                self.overseer.Worker30.encounters += 1
                                if self.needs_sleep():
                                    await self.random_sleep(2.0, 5.0)
                                encountered = True
                            else:
                                if sb_detector:
                                    sb_detector.add_encounter_miss(self.account)
                        except CancelledError:
                            db_proc.add(normalized)
                            raise
                        except Exception as e:
                            self.log.warning('{} during encounter by {}', e.__class__.__name__, self.username)
                            raise e

                    if not encountered and conf.PGSCOUT_ENDPOINT:
                        async with ClientSession(loop=LOOP) as session:
                            encountered = await self.pgscout(session, normalized, pokemon.spawn_point_id)

                if should_notify:
                    LOOP.create_task(self.notifier.notify(normalized, normalized['time_of_day']))

                db_proc.add(normalized)

            if self.passive_scan_gym:
                priority_fort = self.prioritize_forts(map_cell.forts)
            else:
                priority_fort = None

            for fort in map_cell.forts:
                if not fort.enabled:
                    continue
                forts_seen += 1
                if not self.in_bounds(fort.latitude, fort.longitude):
                    continue
                if fort.type == 1:  # pokestops
                    if fort.HasField('lure_info'):
                        norm = self.normalize_lured(fort, request_time_ms)
                        if sb_detector:
                            sb_detector.add_sighting(self.account, norm)
                        pokemon_seen += 1
                        if norm not in SIGHTING_CACHE:
                            SIGHTING_CACHE.add(norm)
                            db_proc.add(norm)
                    if (self.player_level <= 1 or
                            (self.pokestops and self.bag_items < self.item_capacity and
                                time() > self.next_spin and
                                (not conf.SMART_THROTTLE or
                                    self.smart_throttle(2)))):
                        cooldown = fort.cooldown_complete_timestamp_ms
                        if not cooldown or time() > cooldown / 1000:
                            await self.spin_pokestop(fort)
                    if fort.id not in FORT_CACHE.pokestops:
                        pokestop = self.normalize_pokestop(fort)
                        db_proc.add(pokestop)
                else:
                    try:
                        gymexists = FORT_CACHE.gyms[fort.id]
                    except KeyError:
                        gymexists = False

                    if not gymexists or FORT_CACHE.gyms[fort.id]['weather_cell_id'] is None:
                        fort_weather_cell = CellId.from_lat_lng(LatLng.from_degrees(fort.latitude,fort.longitude)).parent(10).id()
                    else:
                        fort_weather_cell = FORT_CACHE.gyms[fort.id]['weather_cell_id']

                    normalized_fort = self.normalize_gym(fort, fort_weather_cell)
                    is_target_gym = (scan_gym_external_id == fort.id)
                    should_update_gym = is_target_gym

                    if is_target_gym:
                        seen_gym = True

                    self.overseer.WorkerRaider.add_gym(normalized_fort)

                    if (scan_gym_external_id or not self.has_raiders) and fort not in FORT_CACHE:
                        should_update_gym = True

                    if (is_target_gym or
                            (priority_fort and
                                priority_fort.id == fort.id)):

                        needs_name = (conf.GYM_NAMES and (fort.id not in FORT_CACHE.gym_info))
                        needs_defenders = conf.GYM_DEFENDERS

                        if needs_name or needs_defenders:
                            gym = await self.gym_get_info(normalized_fort)
                            if gym:
                                should_update_gym = True
                                self.log.info('Got gym info for {}', normalized_fort["name"])

                    if should_update_gym:
                        db_proc.add(normalized_fort)

                        if conf.NOTIFY_GYMS_WEBHOOK:
                            LOOP.create_task(self.notifier.webhook_gym(gym))

                    if fort.HasField('raid_info'):
                        if fort not in RAID_CACHE:
                            if WEATHER_CACHE[fort_weather_cell] != None:
                                weather_cond = WEATHER_CACHE[fort_weather_cell]['condition']
                            else:
                                weather_cond = 0
                            normalized_raid = self.normalize_raid(fort, weather_cond)
                            RAID_CACHE.add(normalized_raid)
                            if normalized_raid['time_end'] > int(time()):
                                normalized_fort["park"] = FORT_CACHE.park.get(fort.id) if FORT_CACHE.park.get(fort.id) \
                                    else ""
                                if conf.NOTIFY_RAIDS:
                                    LOOP.create_task(self.notifier.notify_raid(normalized_raid, normalized_fort))
                                if conf.NOTIFY_RAIDS_WEBHOOK:
                                    LOOP.create_task(self.notifier.webhook_raid(normalized_raid, normalized_fort))
                            db_proc.add(normalized_raid)

            if more_points and (map_cell.s2_cell_id not in self.more_point_cell_cache):
                self.more_point_cell_cache.add(map_cell.s2_cell_id)
                for p in map_cell.spawn_points:
                    points_seen += 1
                    if not self.in_bounds(p.latitude, p.longitude):
                        continue
                    p = p.latitude, p.longitude
                    if spawns.have_point(p):
                        continue
                    spawns.add_cell_point(p)

        if spawn_id and not encounter_id:
            db_proc.add({
                'type': 'target',
                'seen': seen_target,
                'spawn_id': spawn_id})

        if (conf.INCUBATE_EGGS and self.unused_incubators
                and self.eggs and self.smart_throttle()):
            await self.incubate_eggs()

        if pokemon_seen > 0:
            self.error_code = ':'
            self.total_seen += pokemon_seen
            self.g['seen'] += pokemon_seen
            self.empty_visits = 0
        else:
            if (not gym) and (not encounter_id):
                self.empty_visits += 1
            if sb_detector:
                sb_detector.add_empty_visit(self.account)
            if forts_seen == 0:
                self.log.warning('Nothing seen by {}. Speed: {:.2f}', self.username, self.speed)
                self.error_code = '0 SEEN'
            else:
                self.error_code = ','
            if self.empty_visits > 3 and not bootstrap:
                reason = '{} empty visits'.format(self.empty_visits)
                await self.swap_account(reason)
        self.visits += 1

        if self.worker_dict is not None:
            self.worker_dict.update([(self.worker_no,
                (point, start, self.speed, self.total_seen,
                self.visits, pokemon_seen))])
        self.log.info(
            'Point processed, {} Pokemon and {} forts seen by {}!',
            pokemon_seen,
            forts_seen,
            self.username
        )

        await self.update_accounts_dict()
        self.handle = LOOP.call_later(60, self.unset_code)

        if gmo_success:
            if not seen_encounter:
                return -1

            if not seen_gym:
                if forts_seen > 0:
                    return -1
                else:
                    return 0

        return pokemon_seen + forts_seen + points_seen

    async def visit_nearest_pokestop(self, from_point):
        while self.overseer.running:
            closest = float('inf')
            chosen_point = None
            for psid, point in FORT_CACHE.pokestops.items():
                distance = get_distance(from_point, point)
                if distance < closest:
                    closest = distance
                    chosen_point = point
                if closest < 500:
                    break
            if chosen_point:
                closest = get_distance(from_point, point, UNIT)
                time_needed = 3600.0 * closest / conf.SPEED_LIMIT
                if self.last_request > 0 and time_needed > 300.0:
                    return False
                self.log.info("{}(Lv.{}) is going to {}{} for a spin which is {:.2f} {} away and will take {}s.",
                        self.username, self.player_level,
                        FORT_CACHE.pokestop_names.get(psid),
                        chosen_point, closest, conf.SPEED_UNIT, int(time_needed))
                await sleep(time_needed, loop=LOOP)
                return await self.visit(chosen_point, visiting_pokestop=True)
            else:
                self.log.info("{} is needs a spin but couldn't find a spot.", self.username)
                return False

    async def visit_encounter(self, point, sighting):
        self.handle.cancel()
        start = time()
        if sb_detector:
            sb_detector.add_visit(self.account)
        try:
            if await self.encounter(sighting, sighting['spawn_point_id']):
                self.overseer.Worker30.encounters += 1
                if sb_detector:
                    sb_detector.add_sighting(self.account, sighting)
            else:
                if sb_detector:
                    sb_detector.add_encounter_miss(self.account)
        except CancelledError:
            db_proc.add(sighting)
            raise
        except Exception as e:
            self.log.warning('{} during encounter by {}', e.__class__.__name__, self.username)
            raise e

        should_notify = self.should_notify(sighting)
        if should_notify:
            LOOP.create_task(self.notifier.notify(sighting, sighting['time_of_day']))

        db_proc.add(sighting)
        self.last_gmo = self.last_request

        pokemon_seen = 0

        if self.worker_dict is not None:
            self.worker_dict.update([(self.worker_no,
                (point, start, self.speed, self.total_seen,
                self.visits, pokemon_seen))])

        await self.update_accounts_dict()
        self.handle = LOOP.call_later(60, self.unset_code)
        return 1

    def should_skip_sighting(self, sighting, cache):
        # Check if already marked for save as sighting
        if sighting in cache:
            return True
        elif 'expire_timestamp' in sighting:
            cache.add(sighting)
            if sighting.get('expire_timestamp',0) <= time():
                return True
        return False

    def should_notify(self, sighting):
        return (conf.NOTIFY and self.notifier.eligible(sighting))

    def should_encounter(self, sighting, should_notify):
        encounter_conf = conf.ENCOUNTER
        encounter_whitelisted = (encounter_conf == 'all'
                or (encounter_conf == 'some'
                    and sighting['pokemon_id'] in conf.ENCOUNTER_IDS))
        should_notify_with_iv = (should_notify and not conf.IGNORE_IVS)
        return (encounter_whitelisted or should_notify_with_iv)

    async def pgscout(self, session, pokemon, spawn_id):
        PGScout_address=next(self.PGScout_cycle)
        try:
            async with session.get(
                    PGScout_address,
                    params={'pokemon_id': pokemon['pokemon_id'],
                            'encounter_id': pokemon['encounter_id'],
                            'spawn_point_id': spawn_id,
                            'latitude': str(pokemon['lat']),
                            'longitude': str(pokemon['lon'])},
                    timeout=conf.PGSCOUT_TIMEOUT) as resp:
                response = await resp.json(loads=json_loads)
            try:
                pokemon['move_1'] = response['move_1']
                pokemon['move_2'] = response['move_2']
                pokemon['individual_attack'] = response.get('iv_attack',0)
                pokemon['individual_defense'] = response.get('iv_defense',0)
                pokemon['individual_stamina'] = response.get('iv_stamina',0)
                pokemon['height'] = response['height']
                pokemon['weight'] = response['weight']
                pokemon['gender'] = response['gender']
                pokemon['form'] = response.get('form')
                pokemon['cp'] = response.get('cp')
                pokemon['level'] = calc_pokemon_level(response.get('cp_multiplier'))
                return True
            except KeyError:
                self.log.error('Missing Pokemon data in PGScout response.')
        except Exception:
            self.log.exception('PGScout Request Error.')
        return False


    def smart_throttle(self, requests=1):
        try:
            # https://en.wikipedia.org/wiki/Linear_equation#Two_variables
            # e.g. hashes_left > 2.25*seconds_left+7.5, spare = 0.05, max = 150
            spare = conf.SMART_THROTTLE * HashServer.status['maximum']
            hashes_left = HashServer.status['remaining'] - requests
            usable_per_second = (HashServer.status['maximum'] - spare) / 60
            seconds_left = HashServer.status['period'] - time()
            return hashes_left > usable_per_second * seconds_left + spare
        except (TypeError, KeyError):
            return False

    async def gym_get_info(self, gym):
        self.error_code = 'G'

        # randomize location up to ~1.4 meters
        self.simulate_jitter(amount=0.00001)

        request = self.api.create_request()
        request.gym_get_info(gym_id = gym['external_id'],
                             player_lat_degrees = self.location[0],
                             player_lng_degrees = self.location[1],
                             gym_lat_degrees = gym['lat'],
                             gym_lng_degrees = gym['lon'])
        responses = await self.call(request, action=1)

        info = responses['GYM_GET_INFO']
        name = info.name
        result = info.result or 0

        if result == 1:
            try:
                gym['name'] = name
                gym['url'] = info.url.replace('http:','https:')

                for gym_defender in info.gym_status_and_defenders.gym_defender:
                    normalized_defender = self.normalize_gym_defender(gym_defender)
                    gym['gym_defenders'].append(normalized_defender)

            except KeyError as e:
                self.log.error('Missing Gym data in gym_get_info response. {}',e)
            except Exception as e:
                self.log.error('Unknown error: in gym_get_info: {}',e)

        elif result == 2:
            distance = get_distance(self.location, (gym['lat'], gym['lon']))
            self.log.info('The server said {} was out of gym details range. {:.1f}m {:.1f}{}',
                name, distance, self.speed, UNIT_STRING)

        self.error_code = '!'

        return gym

    async def spin_pokestop(self, pokestop):
        self.error_code = '$'
        pokestop_location = pokestop.latitude, pokestop.longitude

        # randomize location up to ~1.5 meters
        self.simulate_jitter(amount=0.00001)

        distance = get_distance(self.location, pokestop_location)
        # permitted interaction distance - 4 (for some jitter leeway)
        # estimation of spinning speed limit
        if distance > 31 or self.speed > SPINNING_SPEED_LIMIT:
            self.error_code = '!'
            return False

        #adding a short sleep period increases spinning success
        await self.random_sleep(0.8, 1.8)

        request = self.api.create_request()
        request.fort_details(fort_id = pokestop.id,
                             latitude = pokestop_location[0],
                             longitude = pokestop_location[1])
        responses = await self.call(request, action=1.3)
        name = responses['FORT_DETAILS'].name
        try:
            normalized = self.normalize_pokestop(pokestop)
            normalized['name'] = name
            normalized['url'] = responses['FORT_DETAILS'].image_urls[0].replace('http:','https:')
            if pokestop.id not in FORT_CACHE.pokestop_names:
                db_proc.add(normalized)
        except KeyError:
            self.log.error("Missing Pokestop data in fort_details response. {}".format(responses))
        except Exception as e:
            self.log.error("Unexpector error in spin_pokestop! {}", e)

        request = self.api.create_request()
        request.fort_search(fort_id = pokestop.id,
                            player_latitude = self.location[0],
                            player_longitude = self.location[1],
                            fort_latitude = pokestop_location[0],
                            fort_longitude = pokestop_location[1])
        responses = await self.call(request, action=3)

        try:
            result = responses['FORT_SEARCH'].result
        except KeyError:
            self.log.warning('Invalid Pokéstop spinning response.')
            self.error_code = '!'
            return

        if result == 1:
            self.log.info('{} spun {}.', self.username, name)
            try:
                inventory_items = responses['GET_INVENTORY'].inventory_delta.inventory_items
                for item in inventory_items:
                    level = item.inventory_item_data.player_stats.level
                    if level and self.player_level and level > self.player_level:
                        # level_up_rewards if level has changed
                        request = self.api.create_request()
                        request.level_up_rewards(level=level)
                        await self.call(request)
                        self.log.info('{} leveled up to Lv.{}, get rewards.', self.username, level)
                        self.player_level = level
                        break
            except KeyError:
                pass
        elif result == 2:
            self.log.info('The server said {} was out of spinning range. {:.1f}m {:.1f}{}',
                name, distance, self.speed, UNIT_STRING)
        elif result == 3:
            self.log.warning('{} was in the cooldown period.', name)
        elif result == 4:
            self.log.warning('Could not spin {} because inventory was full. {}',
                name, self.bag_items)
            self.inventory_timestamp = 0
        elif result == 5:
            self.log.warning('Could not spin {} because the daily limit was reached.', name)
            self.pokestops = False
        else:
            self.log.warning('Failed spinning {}: {}', name, result)

        self.next_spin = time() + conf.SPIN_COOLDOWN
        self.error_code = '!'

    async def encounter(self, pokemon, spawn_id):
        distance_to_pokemon = get_distance(self.location, (pokemon['lat'], pokemon['lon']))
        self.log.info("{} is encountering pokemon: {}, {}", self.username, pokemon['pokemon_id'], pokemon['encounter_id'])
        self.error_code = '~'

        if distance_to_pokemon > 48:
            percent = 1 - (47 / distance_to_pokemon)
            lat_change = (self.location[0] - pokemon['lat']) * percent
            lon_change = (self.location[1] - pokemon['lon']) * percent
            self.location = (
                self.location[0] - lat_change,
                self.location[1] - lon_change)
            self.altitude = uniform(self.altitude - 2, self.altitude + 2)
            self.api.set_position(*self.location, self.altitude)
            delay_required = min((distance_to_pokemon * percent) / 8, 1.1)
        else:
            self.simulate_jitter()
            delay_required = 1.1

        if self.needs_sleep():
            await self.random_sleep(delay_required, delay_required + 1.5)

        wait_encounter = ((self.last_encounter + (self.next_encounter_delay*1000)) - int(round(time() * 1000)))/1000

        if wait_encounter > 0:
            self.log.debug('Last encounter for {} was at {}. Current time is {}. Wait time is {}ms', self.username, self.last_encounter,
                           int(round(time() * 1000)), wait_encounter)
            await sleep(wait_encounter, loop=LOOP)

        request = self.api.create_request()
        request = request.encounter(encounter_id=pokemon['encounter_id'],
                                    spawn_point_id=spawn_id,
                                    player_latitude=self.location[0],
                                    player_longitude=self.location[1])

        responses = await self.call(request, action=2.25)

        if wait_encounter < 0:
           self.next_encounter_delay = 0.15 + (wait_encounter + 0.1) if (wait_encounter + 0.1) > 0 else 0.15
        elif self.next_encounter_delay < 0.5:
            self.next_encounter_delay += 0.1
        else:
            self.next_encounter_delay = 0.15

        try:
            encounter = responses.get('ENCOUNTER')
            if not encounter:
                return False
            status = encounter.status

            if status == 8:
                raise SbAccountException("Blocked by anticheat during encounter")

            # Not success
            if status != 1:
                return False
            pdata = encounter.wild_pokemon.pokemon_data
            pokemon['move_1'] = pdata.move_1
            pokemon['move_2'] = pdata.move_2
            pokemon['individual_attack'] = pdata.individual_attack
            pokemon['individual_defense'] = pdata.individual_defense
            pokemon['individual_stamina'] = pdata.individual_stamina
            pokemon['height'] = pdata.height_m
            pokemon['weight'] = pdata.weight_kg
            pokemon['gender'] = pdata.pokemon_display.gender
            pokemon['cp'] = pdata.cp
            pokemon['level'] = calc_pokemon_level(pdata.cp_multiplier)
        except KeyError:
            self.log.error('Missing encounter response.')
            return False
        except SbAccountException as e:
            raise e
        except Exception as e:
            self.log.error("Unexpected error during encounter: {}", e)
            raise e
        self.error_code = '!'

        self.last_encounter = int(round(time() * 1000))
        self.log.debug('Next encounter delay for {} has been set to {}. Last encounter is now {}', self.username, self.next_encounter_delay,
                       self.last_encounter)
        return True

    async def clean_bag(self):
        self.error_code = '|'
        rec_items = {}
        limits = conf.ITEM_LIMITS
        for item, count in self.items.items():
            if item in limits and count > limits[item]:
                discard = count - limits[item]
                if discard > 50:
                    rec_items[item] = randint(50, discard)
                else:
                    rec_items[item] = discard

        removed = 0
        for item, count in rec_items.items():
            request = self.api.create_request()
            request.recycle_inventory_item(item_id=item, count=count)
            responses = await self.call(request, action=2)

            try:
                if responses['RECYCLE_INVENTORY_ITEM'].result != 1:
                    self.log.warning("Failed to remove item {}", item)
                else:
                    removed += count
            except KeyError:
                self.log.warning("Failed to remove item {}", item)
        self.log.info("Removed {} items", removed)
        self.error_code = '!'

    async def incubate_eggs(self):
        # copy the deque, as self.call could modify it as it updates the inventory
        incubators = self.unused_incubators.copy()
        for egg in sorted(self.eggs.values(), key=lambda x: x.egg_km_walked_target):
            if not incubators:
                break

            if egg.egg_incubator_id:
                continue

            inc = incubators.pop()
            if inc.item_id == 901 or egg.egg_km_walked_target > 9:
                request = self.api.create_request()
                request.use_item_egg_incubator(item_id=inc.id, pokemon_id=egg.id)
                responses = await self.call(request, action=4.5)

                try:
                    ret = responses['USE_ITEM_EGG_INCUBATOR'].result
                    if ret == 4:
                        self.log.warning("Failed to use incubator because it was already in use.")
                    elif ret != 1:
                        self.log.warning("Failed to apply incubator {} on {}, code: {}",
                            inc.id, egg.id, ret)
                except (KeyError, AttributeError):
                    self.log.error('Invalid response to USE_ITEM_EGG_INCUBATOR')

        self.unused_incubators = incubators

    async def handle_captcha(self, challenge_url):
        if self.num_captchas >= conf.CAPTCHAS_ALLOWED:
            self.log.error("{} encountered too many CAPTCHAs, removing.", self.username)
            raise CaptchaException

        self.error_code = 'C'
        self.num_captchas += 1
        session = SessionManager.get()

        if not conf.USE_ANTICAPTCHA:
            try:
                params = {
                    'key': conf.CAPTCHA_KEY,
                    'method': 'userrecaptcha',
                    'googlekey': '6LeeTScTAAAAADqvhqVMhPpr_vB9D364Ia-1dSgK',
                    'pageurl': challenge_url,
                    'json': 1
                }
                async with session.post('http://2captcha.com/in.php', params=params) as resp:
                    response = await resp.json(loads=json_loads)
            except CancelledError:
                raise
            except Exception as e:
                self.log.error('Got an error while trying to solve CAPTCHA. '
                               'Check your API Key and account balance.')
                raise CaptchaSolveException from e

            code = response.get('request')
            if response.get('status') != 1:
                if code in ('ERROR_WRONG_USER_KEY', 'ERROR_KEY_DOES_NOT_EXIST', 'ERROR_ZERO_BALANCE'):
                    conf.CAPTCHA_KEY = None
                    self.log.error('2Captcha reported: {}, disabling CAPTCHA solving', code)
                else:
                    self.log.error("Failed to submit CAPTCHA for solving: {}", code)
                raise CaptchaSolveException

            try:
                # Get the response, retry every 5 seconds if it's not ready
                params = {
                    'key': conf.CAPTCHA_KEY,
                    'action': 'get',
                    'id': code,
                    'json': 1
                }
                while True:
                    async with session.get("http://2captcha.com/res.php", params=params, timeout=20) as resp:
                        response = await resp.json(loads=json_loads)
                    if response.get('request') != 'CAPCHA_NOT_READY':
                        break
                    await sleep(5, loop=LOOP)
            except CancelledError:
                raise
            except Exception as e:
                self.log.error('Got an error while trying to solve CAPTCHA. '
                                  'Check your API Key and account balance.')
                raise CaptchaSolveException from e

            token = response.get('request')
            if not response.get('status') == 1:
                self.log.error("Failed to get CAPTCHA response: {}", token)
                raise CaptchaSolveException
        else:
            try:
                acclient = AnticaptchaClient(conf.CAPTCHA_KEY)
                actask = NoCaptchaTaskProxylessTask(challenge_url, '6LeeTScTAAAAADqvhqVMhPpr_vB9D364Ia-1dSgK')
                acjob = acclient.createTask(actask)
                acjob.join()
                token = acjob.get_solution_response()
            except AnticatpchaException as e:
                self.log.error('AntiCaptcha error: {}, {}', e.error_code, e.error_description)
                raise CaptchaException from e
            except Exception as e:
                self.log.error('Other error from anticaptcha')
                raise CaptchaException from e

        request = self.api.create_request()
        request.verify_challenge(token=token)
        await self.call(request, action=4)
        await self.update_accounts_dict()
        self.log.warning("Successfully solved CAPTCHA")

    def simulate_jitter(self, amount=0.00002):
        '''Slightly randomize location, by up to ~3 meters by default.'''
        self.location = randomize_point(self.location, amount=amount)
        self.altitude = uniform(self.altitude - 1, self.altitude + 1)
        self.api.set_position(*self.location, self.altitude)

    async def update_accounts_dict(self):
        self.account['location'] = self.location
        self.account['time'] = self.last_request
        self.account['inventory_timestamp'] = self.inventory_timestamp
        if self.player_level:
            self.account['level'] = self.player_level

        try:
            self.account['auth'] = self.api.auth_provider._access_token
            self.account['expiry'] = self.api.auth_provider._access_token_expiry
        except AttributeError:
            pass

        ACCOUNTS = self.account_dict
        if 'remove' in self.account and self.account['remove']:
            if self.username in ACCOUNTS:
                del ACCOUNTS[self.username]
        else:
            async with self.update_account_lock:
                Account.put(self.account)
            ACCOUNTS[self.username] = self.account

    async def remove_account(self, flag='banned'):
        log_message = ''
        send_webhook = True
        self.error_code = 'REMOVING'
        if flag == 'warn':
            self.account['warn'] = True
            log_message = "Hibernating {} due to warn. Level - {}".format(self.username, self.player_level)
        elif flag == 'sbanned':
            self.account['sbanned'] = True
            log_message = "Hibernating {} due to shadow ban. Level - {}".format(self.username, self.player_level)
        elif flag == 'code3':
            self.account['code3'] = True
            log_message = "Hibernating {} due to code3. Level - {}".format(self.username, self.player_level)
        elif flag == 'credentials':
            self.account['credentials'] = True
            log_message = "Removing {} due to wrong credentials. Level - {}".format(self.username, self.player_level)
        elif flag == 'unverified':
            self.account['unverified'] = True
            log_message = "Removing {} due to unverified email.. Level - {}".format(self.username, self.player_level)
        elif flag == 'security':
            self.account['security'] = True
            log_message = "Removing {} due to security lock. Level - {}".format(self.username, self.player_level)
        elif flag == 'tempdisabled':
            self.account['tempdisabled'] = True
            log_message = "Removing {} due to temp disabled. Level - {}".format(self.username, self.player_level)
        elif flag == 'level30':
            self.account['graduated'] = True
            log_message = "Removing {} from slave pool due to graduation to Lv.30. Level - {}".format(self.username, self.player_level)
            send_webhook = False
        elif flag == 'level1':
            self.account['demoted'] = True
            log_message = "Removing {} from captain pool due to insufficient level. Level - {}".format(self.username, self.player_level)
            send_webhook = False
        else:
            self.account['banned'] = True
            log_message = "Hibernating {} due to ban.".format(self.username, self.player_level)

        self.log.warning(log_message)
        LOOP.create_task(self.notifier.hibernate_webhook(self.username, self.player_level, log_message))

        await self.update_accounts_dict()
        self.username = None
        self.account = None
        await self.new_account(after_remove=True)

    async def bench_account(self):
        self.error_code = 'BENCHING'
        self.log.warning('Swapping {} due to CAPTCHA.', self.username)
        self.account['captcha'] = True
        await self.update_accounts_dict()
        self.captcha_queue.put(self.account)
        await self.new_account()

    async def lock_and_swap(self, minutes):
        try:
            async with self.busy:
                await self.swap_account(reason='long_running', minutes=minutes)
        except InsufficientAccountsException:
            self.log.error("No more accounts available in DB for lock and swap")
        except Exception as e:
            self.log.error('Unexpected exception in lock_and_swap {}', e)

    async def swap_account(self, reason='', minutes=None):
        self.error_code = 'SWAPPING'
        if minutes:
            h, m = divmod(int(minutes), 60)
            if h:
                timestr = '{}h{}m'.format(h, m)
            else:
                timestr = '{}m'.format(m)
            self.log.warning('Swapping {} which had been running for {}.', self.username, timestr)
        else:
            self.log.warning('Swapping out {} because {}.', self.username, reason)
        await self.update_accounts_dict()
        accounts_in_queues = self.account_queue.qsize() + self.captcha_queue.qsize()
        self.account_queue.put(self.account)
        direct_from_db = accounts_in_queues < self.required_extra_accounts()
        await self.new_account(direct_from_db=direct_from_db)

    async def get_account_from_db(self):
        try:
            return await run_threaded(Account.get, self.min_level(), self.max_level())
        except InsufficientAccountsException:
            self.log.error("No more accounts available in DB #1")

    async def new_account(self, after_remove=False, direct_from_db=False):
        if direct_from_db:
            self.account = await self.get_account_from_db()
            if self.account:
                self.log.info('Acquired new account {} direct from DB.', self.account.get('username'))
        else:
            if (conf.CAPTCHA_KEY
                    and (conf.FAVOR_CAPTCHA or self.account_queue.empty())
                    and not self.captcha_queue.empty()):
                self.account = self.captcha_queue.get()
            else:
                try:
                    self.account = self.account_queue.get_nowait()
                except Empty as e:
                    if after_remove:
                        self.log.error("No more accounts available in DB #2")
                    else:
                        self.account = await run_threaded(self.account_queue.get)
                except InsufficientAccountsException:
                    self.log.error("No more accounts available in DB #3")
        if not self.account:
            return
        self.username = self.account['username']
        try:
            self.location = self.account['location'][:2]
        except KeyError:
            self.location = self.get_start_coords()
        self.inventory_timestamp = self.account.get('inventory_timestamp', 0) if self.items else 0
        self.player_level = self.account.get('level')
        self.last_request = self.account.get('time', 0)
        self.last_action = self.last_request
        self.last_gmo = self.last_request
        try:
            self.items = self.account['items']
            self.bag_items = sum(self.items.values())
        except KeyError:
            self.account['items'] = {}
            self.items = self.account['items']
        self.num_captchas = 0
        self.eggs = {}
        self.unused_incubators = deque()
        self.initialize_api()
        self.error_code = None

    def within_distance(self, fort, max_distance=445):
        gym_location = fort.latitude, fort.longitude
        distance = get_distance(self.location, gym_location)

        if distance > max_distance:
            return False

        return True

    def prioritize_forts(self, map_cell_forts):

        # Filter gyms that are nearby
        forts = [ x for x in map_cell_forts if x.type == 0 and self.within_distance(x, max_distance=445)]

        raids_to_check = [ x for x in forts if x.HasField("raid_info") and (x not in RAID_CACHE)]
        gyms_to_check = [ x for x in forts if not x.HasField("raid_info") and (x not in FORT_CACHE)]

        # Order oldest first
        raids_to_check.sort(key=lambda x: x.last_modified_timestamp_ms, reverse=False)
        gyms_to_check.sort(key=lambda x: x.last_modified_timestamp_ms, reverse=False)

        # Prioritize raids over normal gyms
        forts_to_check = raids_to_check +  gyms_to_check

        # Get the head
        fort_to_check = forts_to_check[0] if len(forts_to_check) > 0 else None
        return fort_to_check

    def unset_code(self):
        self.error_code = None

    @staticmethod
    def normalize_pokemon(raw, username=None):
        """Normalizes data coming from API into something acceptable by db"""
        tsm = raw.last_modified_timestamp_ms
        tss = round(tsm / 1000)
        tth = raw.time_till_hidden_ms
        spawn_id = int(raw.spawn_point_id, 16)
        despawn = spawns.get_despawn_time(spawn_id, tss)
        norm = {
            'type': 'pokemon',
            'encounter_id': raw.encounter_id,
            'pokemon_id': raw.pokemon_data.pokemon_id,
            'lat': raw.latitude,
            'lon': raw.longitude,
            'spawn_id': spawn_id,
            'spawn_point_id': raw.spawn_point_id,
            'seen': tss,
            'gender': raw.pokemon_data.pokemon_display.gender,
            'form': raw.pokemon_data.pokemon_display.form,
            'username': username,
            'despawn': despawn,
        }
        if tth > 0 and tth <= 90000:
            norm['expire_timestamp'] = round((tsm + tth) / 1000)
            norm['time_till_hidden'] = tth / 1000
            norm['inferred'] = False
        else:
            if despawn:
                norm['expire_timestamp'] = despawn
                norm['time_till_hidden'] = despawn - tss
                norm['inferred'] = True
            else:
                norm['type'] = 'mystery'
        if raw.pokemon_data.pokemon_display:
            if raw.pokemon_data.pokemon_display.form:
                norm['display'] = raw.pokemon_data.pokemon_display.form
            if raw.pokemon_data.pokemon_display.weather_boosted_condition:
                norm['weather_boosted_condition'] = raw.pokemon_data.pokemon_display.weather_boosted_condition

        return norm

    @classmethod
    def normalize_lured(self, raw, now):
        lure = raw.lure_info
        return {
            'type': 'pokemon',
            'encounter_id': lure.encounter_id,
            'pokemon_id': lure.active_pokemon_id,
            'expire_timestamp': lure.lure_expires_timestamp_ms // 1000,
            'lat': raw.latitude,
            'lon': raw.longitude,
            'spawn_id': 0,
            'spawn_point_id': None,
            'time_till_hidden': (lure.lure_expires_timestamp_ms - now) / 1000,
            'inferred': 'pokestop'
        }

    @staticmethod
    def normalize_gym(raw, s2cellID):
        return {
            'type': 'fort',
            'external_id': raw.id,
            'lat': raw.latitude,
            'lon': raw.longitude,
            'team': raw.owned_by_team,
            'guard_pokemon_id': raw.guard_pokemon_id,
            'sponsor': raw.sponsor,
            'last_modified': raw.last_modified_timestamp_ms // 1000,
            'is_in_battle': raw.is_in_battle,
            'slots_available': raw.gym_display.slots_available,
            'name': None,
            'url': None,
            'total_cp': raw.gym_display.total_gym_cp,
            'gym_defenders': [],
            'weather_cell_id': s2cellID & 0xffffffffffffffff
        }

    @staticmethod
    def normalize_raid(raw, weather):
        obj = {
            'type': 'raid',
            'external_id': raw.raid_info.raid_seed,
            'fort_external_id': raw.id,
            'lat': raw.latitude,
            'lon': raw.longitude,
            'level': raw.raid_info.raid_level,
            'pokemon_id': 0,
            'time_spawn': raw.raid_info.raid_spawn_ms // 1000,
            'time_battle': raw.raid_info.raid_battle_ms // 1000,
            'time_end': raw.raid_info.raid_end_ms // 1000,
            'cp': 0,
            'move_1': 0,
            'move_2': 0,
            'weather' : weather
        }
        if raw.raid_info.HasField('raid_pokemon'):
            obj['pokemon_id'] = raw.raid_info.raid_pokemon.pokemon_id
            obj['cp'] = raw.raid_info.raid_pokemon.cp
            obj['move_1'] = raw.raid_info.raid_pokemon.move_1
            obj['move_2'] = raw.raid_info.raid_pokemon.move_2
        return obj

    @staticmethod
    def normalize_gym_defender(raw):
        pokemon = raw.motivated_pokemon.pokemon
        trainer = raw.trainer_public_profile

        obj = {
            'type': 'gym_defender',
            'external_id': pokemon.id,
            'pokemon_id': pokemon.pokemon_id,
            'owner_name': pokemon.owner_name,
            'owner_level': trainer.level,
            'nickname': pokemon.nickname,
            'cp': pokemon.cp,
            'stamina': pokemon.stamina,
            'stamina_max': pokemon.stamina_max,
            'atk_iv': pokemon.individual_attack,
            'def_iv': pokemon.individual_defense,
            'sta_iv': pokemon.individual_stamina,
            'move_1': pokemon.move_1,
            'move_2': pokemon.move_2,
            'battles_attacked': pokemon.battles_attacked,
            'battles_defended': pokemon.battles_defended,
            'num_upgrades': 0,
        }

        if hasattr(pokemon, 'num_upgrades'):
            obj['num_upgrades'] = pokemon.num_upgrades

        return obj


    @staticmethod
    def normalize_pokestop(raw):
        return {
            'type': 'pokestop',
            'external_id': raw.id,
            'lat': raw.latitude,
            'lon': raw.longitude,
            'name': None,
            'url': None
        }

    @staticmethod
    async def random_sleep(minimum=10.1, maximum=14, loop=LOOP):
        """Sleeps for a bit"""
        await sleep(uniform(minimum, maximum), loop=loop)

    @property
    def start_time(self):
        return self.api.start_time

    @property
    def status(self):
        """Returns status message to be displayed in status screen"""
        if self.error_code:
            msg = self.error_code
        else:
            msg = 'P{seen}'.format(
                seen=self.total_seen
            )
        return '[W{worker_no}: {msg}]'.format(
            worker_no=self.worker_no,
            msg=msg
        )

    @property
    def authenticated(self):
        try:
            return self.api.auth_provider.authenticated
        except AttributeError:
            return False


class HandleStub:
    def cancel(self):
        pass


class EmptyGMOException(Exception):
    """Raised when the GMO response is empty."""


class CaptchaException(Exception):
    """Raised when a CAPTCHA is needed."""


class CaptchaSolveException(Exception):
    """Raised when solving a CAPTCHA has failed."""

class OverseerNotRunningException(Exception):
    """Raised when overseer is no longer running."""
    pass
