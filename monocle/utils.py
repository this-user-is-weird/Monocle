import random
import socket

from os import mkdir
from os.path import join, exists
from sys import platform
from asyncio import sleep, Semaphore
from math import sqrt
from uuid import uuid4
from enum import Enum
from csv import DictReader
from cyrandom import choice, shuffle, uniform
from time import time
from s2sphere import Cell as S2Cell, LatLng, CellId as S2CellId
from pickle import dump as pickle_dump, load as pickle_load, HIGHEST_PROTOCOL

from geopy import Point
from geopy.distance import distance
from aiopogo import utilities as pgoapi_utils
from pogeo import get_distance

from . import bounds, sanitized as conf

# iPhones 5 + 5C (4S is really not playable)
IPHONES = {'iPhone7,1': 'N56AP',
           'iPhone7,2': 'N61AP',
           'iPhone8,1': 'N71AP',
           'iPhone8,2': 'N66AP',
           'iPhone8,4': 'N69AP',
           'iPhone9,1': 'D10AP',
           'iPhone9,2': 'D11AP',
           'iPhone9,3': 'D101AP',
           'iPhone9,4': 'D111AP',
           'iPhone10,1': 'D20AP',
           'iPhone10,2': 'D21AP',
           'iPhone10,3': 'D22AP',
           'iPhone10,4': 'D201AP',
           'iPhone10,5': 'D211AP',
           'iPhone10,6': 'D221AP'}


class Units(Enum):
    miles = 1
    kilometers = 2
    meters = 3


def best_factors(n):
    return next(((i, n//i) for i in range(int(n**0.5), 0, -1) if n % i == 0))


def percentage_split(seq, percentages):
    percentages[-1] += 1.0 - sum(percentages)
    prv = 0
    size = len(seq)
    cum_percentage = 0
    for p in percentages:
        cum_percentage += p
        nxt = int(cum_percentage * size)
        yield seq[prv:nxt]
        prv = nxt


def get_start_coords(worker_no, grid=conf.GRID, bounds=bounds):
    """Returns center of square for given worker"""
    per_column = int((grid[0] * grid[1]) / grid[0])

    column = worker_no % per_column
    row = int(worker_no / per_column)
    part_lat = (bounds.south - bounds.north) / grid[0]
    part_lon = (bounds.east - bounds.west) / grid[1]
    start_lat = bounds.north + part_lat * row + part_lat / 2
    start_lon = bounds.west + part_lon * column + part_lon / 2
    return start_lat, start_lon


def float_range(start, end, step):
    """range for floats, also capable of iterating backwards"""
    if start > end:
        while end <= start:
            yield start
            start += -step
    else:
        while start <= end:
            yield start
            start += step


def get_gains(dist=70):
    """Returns lat and lon gain

    Gain is space between circles.
    """
    start = Point(*bounds.center)
    base = dist * sqrt(3)
    height = base * sqrt(3) / 2
    dis_a = distance(meters=base)
    dis_h = distance(meters=height)
    lon_gain = dis_a.destination(point=start, bearing=90).longitude
    lat_gain = dis_h.destination(point=start, bearing=0).latitude
    return abs(start.latitude - lat_gain), abs(start.longitude - lon_gain)


def round_coords(point, precision, _round=round):
    return _round(point[0], precision), _round(point[1], precision)


def get_bootstrap_points(bounds):
    coords = []
    if bounds.multi:
        for b in bounds.polygons:
            coords.extend(get_bootstrap_points(b))
        return coords
    lat_gain, lon_gain = get_gains(conf.BOOTSTRAP_RADIUS)
    west, east = bounds.west, bounds.east
    bound = bool(bounds)
    for map_row, lat in enumerate(
        float_range(bounds.south, bounds.north, lat_gain)
    ):
        row_start_lon = west
        if map_row % 2 != 0:
            row_start_lon -= 0.5 * lon_gain
        for lon in float_range(row_start_lon, east, lon_gain):
            point = lat, lon
            if not bound or point in bounds:
                coords.append(point)
    shuffle(coords)
    return coords


def get_device_info(account):
    device_info = {'brand': 'Apple',
                   'device': 'iPhone',
                   'manufacturer': 'Apple'}
    try:
        if account['iOS'].startswith('1'):
            device_info['product'] = 'iOS'
        else:
            device_info['product'] = 'iPhone OS'
        device_info['hardware'] = account['model'] + '\x00'
        device_info['model'] = IPHONES[account['model']] + '\x00'
    except (KeyError, AttributeError):
        account = generate_device_info(account)
        return get_device_info(account)
    device_info['version'] = account['iOS']
    device_info['device_id'] = account['id']
    return device_info


def generate_device_info(account):
    #ios9 = ('9.0', '9.0.1', '9.0.2', '9.1', '9.2', '9.2.1', '9.3', '9.3.1', '9.3.2', '9.3.3', '9.3.4', '9.3.5')
    # 10.0 was only for iPhone 7 and 7 Plus, and is rare
    #ios10 = ('10.0.1', '10.0.2', '10.0.3', '10.1', '10.1.1', '10.2', '10.2.1', '10.3', '10.3.1', '10.3.2', '10.3.3')
    ios11 = ('11.0.1', '11.0.2', '11.0.3', '11.1', '11.1.1')

    devices = tuple(IPHONES.keys())
    account['model'] = choice(devices)

    account['id'] = uuid4().hex

    account['iOS'] = choice(ios11)

    return account

def get_current_hour(now=None, _time=time):
    now = now or _time()
    return round(now - (now % 3600))


def time_until_time(seconds, seen=None, _time=time):
    current_seconds = seen or _time() % 3600
    if current_seconds > seconds:
        return seconds + 3600 - current_seconds
    elif current_seconds + 3600 < seconds:
        return seconds - 3600 - current_seconds
    else:
        return seconds - current_seconds


def get_address():
    if conf.MANAGER_ADDRESS:
        return conf.MANAGER_ADDRESS
    if platform == 'win32':
        return r'\\.\pipe\monocle'
    if hasattr(socket, 'AF_UNIX'):
        return join(conf.DIRECTORY, 'monocle.sock')
    return ('127.0.0.1', 5001)


def load_pickle(name, raise_exception=False):
    location = join(conf.DIRECTORY, 'pickles', '{}.pickle'.format(name))
    try:
        with open(location, 'rb') as f:
            return pickle_load(f)
    except (FileNotFoundError, EOFError):
        if raise_exception:
            raise FileNotFoundError
        else:
            return None


def dump_pickle(name, var):
    folder = join(conf.DIRECTORY, 'pickles')
    try:
        mkdir(folder)
    except FileExistsError:
        pass
    except Exception as e:
        raise OSError("Failed to create 'pickles' folder, please create it manually") from e

    location = join(folder, '{}.pickle'.format(name))
    with open(location, 'wb') as f:
        pickle_dump(var, f, HIGHEST_PROTOCOL)


def randomize_point(point, amount=0.0003, randomize=uniform):
    '''Randomize point, by up to ~47 meters by default.'''
    lat, lon = point
    return (
        randomize(lat - amount, lat + amount),
        randomize(lon - amount, lon + amount)
    )


def calc_pokemon_level(cp_multiplier):
    if cp_multiplier < 0.734:
        pokemon_level = (58.35178527 * cp_multiplier * cp_multiplier - 2.838007664 * cp_multiplier + 0.8539209906)
    else:
        pokemon_level = 171.0112688 * cp_multiplier - 95.20425243
    pokemon_level = int((round(pokemon_level) * 2) / 2)
    return pokemon_level

def get_google_maps_key():
    if isinstance(conf.GOOGLE_MAPS_KEY, str):
        return conf.GOOGLE_MAPS_KEY
    return random.choice(conf.GOOGLE_MAPS_KEY)

def get_static_map_url(lat, lon, icon=None):
        center = '{},{}'.format(lat, lon)
        query_center = 'center={}'.format(center)
        query_markers = 'markers=color:red%7C{}'.format(center)
        query_size = 'size={}x{}'.format('250', '125')
        query_zoom = 'zoom={}'.format('15')
        query_maptype = 'maptype={}'.format('roadmap')

        if icon is not None:
            query_markers = 'markers=icon:{icon_url}%7C{lat},{lon}'.format(
                icon_url = icon,
                lat = lat,
                lon = lon)

        url = ('https://maps.googleapis.com/maps/api/staticmap?' +
                query_center + '&' + query_markers + '&' +
                query_maptype + '&' + query_size + '&' + query_zoom)

        if conf.GOOGLE_MAPS_KEY:
            url += "&key={}".format(get_google_maps_key())
        return url

def get_gmaps_link(lat, lon):
    return "http://maps.google.com/maps?q={},{}".format(lat, lon)

# Returns a String link to Apple Maps Pin at the location
# Code from PokeAlarm
def get_applemaps_link(lat, lon):
    latLon = '{},{}'.format(repr(lat), repr(lon))
    return 'http://maps.apple.com/maps?daddr={}&z=10&t=s&dirflg=w'.format(latLon)

class FlexibleSemaphore(Semaphore):
    def increment(self, by=1):
        self._value += by 
        for idx in range(by):
            self._wake_up_next()

    def decrement(self, by=1):
        self._value -= by 

    def value(self):
        return self._value

def get_vertex(cell, v):
    vertex = LatLng.from_point(cell.get_vertex(v))
    return (vertex.lat().degrees, vertex.lng().degrees)


def get_s2_cell_as_polygon(lat, lon, level=12):
    cell = S2Cell(S2CellId.from_lat_lng(LatLng.from_degrees(lat, lon)).parent(level))
    return [(get_vertex(cell, v)) for v in range(0, 4)]
