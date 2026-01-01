# -*- coding: UTF-8 -*-
#
# Copyright (C) 2020, Team Kodi
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
# pylint: disable=missing-docstring
#
# This is based on the metadata.tvmaze scrapper by Roman Miroshnychenko aka Roman V.M.

"""Functions to process data"""

from __future__ import absolute_import, unicode_literals

import re
import json
from xbmc import Actor, VideoStreamDetail
from collections import namedtuple
from .utils import safe_get, logger
from . import settings, api_utils

try:
    from typing import Optional, Tuple, Text, Dict, List, Any  # pylint: disable=unused-import
    from xbmcgui import ListItem  # pylint: disable=unused-import
    InfoType = Dict[Text, Any]  # pylint: disable=invalid-name
except ImportError:
    pass


SOURCE_SETTINGS = settings.getSourceSettings()
BASE_URL = 'https://api.themoviedb.org/3/{}'
FIND_URL = BASE_URL.format('find/{}')
TAG_RE = re.compile(r'<[^>]+>')

# Regular expressions are listed in order of priority.
# "TMDB" provider is preferred than other providers (IMDB and TheTVDB),
# because external providers IDs need to be converted to TMDB_ID.
SHOW_ID_REGEXPS = (
    r'(themoviedb)\.org/tv/(\d+).*/episode_group/(.*)',   # TMDB_http_link
    r'(themoviedb)\.org/tv/(\d+)',                        # TMDB_http_link
    r'(themoviedb)\.org/./tv/(\d+)',                      # TMDB_http_link
    r'(tmdb)\.org/./tv/(\d+)',                            # TMDB_http_link
    r'(imdb)\.com/.+/(tt\d+)',                            # IMDB_http_link
    r'(thetvdb)\.com.+&id=(\d+)',                         # TheTVDB_http_link
    r'(thetvdb)\.com/series/(\d+)',                       # TheTVDB_http_link
    r'(thetvdb)\.com/api/.*series/(\d+)',                 # TheTVDB_http_link
    r'(thetvdb)\.com/.*?"id":(\d+)',                      # TheTVDB_http_link
)


SUPPORTED_ARTWORK_TYPES = {'poster', 'banner'}
IMAGE_SIZES = ('large', 'original', 'medium')
CLEAN_PLOT_REPLACEMENTS = (
    ('<b>', '[B]'),
    ('</b>', '[/B]'),
    ('<i>', '[I]'),
    ('</i>', '[/I]'),
    ('</p><p>', '[CR]'),
)
VALIDEXTIDS = ['tmdb_id', 'imdb_id', 'tvdb_id']

UrlParseResult = namedtuple(
    'UrlParseResult', ['provider', 'show_id', 'ep_grouping'])


def get_pinyin_initials(text):
    # TV Shows scraper doesn't have direct access to the daemon client code
    # We need to implement a simple socket client here or import from a shared location
    # Since we can't easily share code between addons, we'll implement a minimal client here
    if not text:
        return ""
    
    try:
        # Check if daemon port is available
        import xbmcgui
        port_prop = xbmcgui.Window(10000).getProperty('TMDB_TV_OPTIMIZATION_SERVICE_PORT')
        if not port_prop:
            # Try to start daemon via RunScript? 
            # The movie scraper daemon is shared, so we can try to wake it up
            import xbmc
            import time
            addon_id = 'metadata.tvshows.tmdb.cn.optimization'
            script_path = 'special://home/addons/{}/daemon.py'.format(addon_id)
            xbmc.executebuiltin('RunScript({})'.format(script_path))
            
            # Wait for port to be available (max 5 seconds)
            for _ in range(50):
                if xbmcgui.Window(10000).getProperty('TMDB_TV_OPTIMIZATION_SERVICE_PORT'):
                    port_prop = xbmcgui.Window(10000).getProperty('TMDB_TV_OPTIMIZATION_SERVICE_PORT')
                    break
                time.sleep(0.1)
            
            if not port_prop:
                return "" # Still not available

        service_port = int(port_prop)
        payload = {'pinyin': text}
        
        import socket
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(5)
            s.connect(('127.0.0.1', service_port))
            s.sendall(json.dumps(payload).encode('utf-8'))
            
            data = b""
            while True:
                chunk = s.recv(4096)
                if not chunk:
                    break
                data += chunk
            
            if not data:
                return ""
                
            response = json.loads(data)
            return response.get('result', "")
    except:
        return ""


def _clean_plot(plot):
    # type: (Text) -> Text
    """Replace HTML tags with Kodi skin tags"""
    for repl in CLEAN_PLOT_REPLACEMENTS:
        plot = plot.replace(repl[0], repl[1])
    plot = TAG_RE.sub('', plot)
    return plot


def _set_cast(cast_info, vtag):
    # type: (InfoType, ListItem) -> ListItem
    """Save cast info to list item"""
    imagerooturl, previewrooturl = settings.loadBaseUrls()
    cast = []
    for item in cast_info:
        actor = {
            'name': item['name'],
            'role': item.get('character', item.get('character_name', '')),
            'order': item['order'],
        }
        thumb = None
        if safe_get(item, 'profile_path') is not None:
            thumb = imagerooturl + item['profile_path']
        cast.append(Actor(actor['name'], actor['role'], actor['order'], thumb))
    vtag.setCast(cast)


def _get_credits(show_info):
    # type: (InfoType) -> List[Text]
    """Extract show creator(s) and writer(s) from show info"""
    credits = []
    for item in show_info.get('created_by', []):
        credits.append(item['name'])
    for item in show_info.get('credits', {}).get('crew', []):
        isWriter = item.get('job', '').lower() == 'writer' or item.get(
            'department', '').lower() == 'writing'
        if isWriter and item.get('name') not in credits:
            credits.append(item['name'])
    return credits


def _get_directors(episode_info):
    # type: (InfoType) -> List[Text]
    """Extract episode writer(s) from episode info"""
    directors_ = []
    for item in episode_info.get('credits', {}).get('crew', []):
        if item.get('job') == 'Director':
            directors_.append(item['name'])
    return directors_


def _set_unique_ids(ext_ids, vtag):
    # type: (Dict, ListItem) -> ListItem
    """Extract unique ID in various online databases"""
    return_ids = {}
    for key, value in ext_ids.items():
        if key in VALIDEXTIDS and value:
            if key == 'tmdb_id':
                isTMDB = True
            else:
                isTMDB = False
            shortkey = key[:-3]
            str_value = str(value)
            vtag.setUniqueID(str_value, type=shortkey, isdefault=isTMDB)
            return_ids[shortkey] = str_value
    return return_ids


def _set_rating(the_info, vtag):
    # type: (InfoType, ListItem) -> None
    """Set show/episode rating"""
    first = True
    for rating_type in SOURCE_SETTINGS["RATING_TYPES"]:
        logger.debug('adding rating type of %s' % rating_type)
        rating = float(the_info.get('ratings', {}).get(
            rating_type, {}).get('rating', '0'))
        votes = int(the_info.get('ratings', {}).get(
            rating_type, {}).get('votes', '0'))
        logger.debug("adding rating of %s and votes of %s" %
                     (str(rating), str(votes)))
        if rating > 0:
            vtag.setRating(rating, votes=votes,
                           type=rating_type, isdefault=first)
            first = False


def _add_season_info(show_info, vtag):
    # type: (InfoType, ListItem) -> None
    """Add info for show seasons"""
    for season in show_info['seasons']:
        logger.debug('adding information for season %s to list item' %
                     season['season_number'])
        vtag.addSeason(season['season_number'],
                       safe_get(season, 'name', ''))
        for image_type, image_list in season.get('images', {}).items():
            if image_type == 'posters':
                destination = 'poster'
            else:
                destination = image_type
            for image in image_list:
                theurl, previewurl = get_image_urls(image)
                if theurl:
                    vtag.addAvailableArtwork(
                        theurl, arttype=destination, preview=previewurl, season=season['season_number'])


def _get_names(item_list):
    # type: (List) -> None
    """Get names from a list of dicts"""
    items = []
    for item in item_list:
        items.append(item['name'])
    return items


def get_image_urls(image):
    # type: (Dict) -> Tuple[Text, Text]
    """Get image URLs from image information"""
    imagerooturl, previewrooturl = settings.loadBaseUrls()
    if image.get('file_path', '').endswith('.svg'):
        return None, None
    if image.get('type') == 'fanarttv':
        theurl = image['file_path']
        previewurl = theurl.replace(
            '.fanart.tv/fanart/', '.fanart.tv/preview/')
    else:
        theurl = imagerooturl + image['file_path']
        previewurl = previewrooturl + image['file_path']
    return theurl, previewurl


def set_show_artwork(show_info, list_item):
    # type: (InfoType, ListItem) -> ListItem
    """Set available images for a show"""
    vtag = list_item.getVideoInfoTag()
    for image_type, image_list in show_info.get('images', {}).items():
        if image_type == 'backdrops':
            fanart_list = []
            for image in image_list:
                theurl, previewurl = get_image_urls(image)
                if (image.get('iso_639_1') != None and image.get('iso_639_1').lower() != 'xx') and SOURCE_SETTINGS["CATLANDSCAPE"] and theurl:
                    vtag.addAvailableArtwork(
                        theurl, arttype="landscape", preview=previewurl)
                elif theurl:
                    fanart_list.append({'image': theurl})
            if fanart_list:
                list_item.setAvailableFanart(fanart_list)
        else:
            if image_type == 'posters':
                destination = 'poster'
            elif image_type == 'logos':
                destination = 'clearlogo'
            else:
                destination = image_type
            for image in image_list:
                theurl, previewurl = get_image_urls(image)
                if theurl:
                    vtag.addAvailableArtwork(
                        theurl, arttype=destination, preview=previewurl)
    return list_item


def add_main_show_info(list_item, show_info, full_info=True):
    # type: (ListItem, InfoType, bool) -> ListItem
    """Add main show info to a list item"""
    imagerooturl, previewrooturl = settings.loadBaseUrls()
    vtag = list_item.getVideoInfoTag()
    original_name = show_info.get('original_name')
    if SOURCE_SETTINGS["KEEPTITLE"] and original_name:
        showname = original_name
    else:
        showname = show_info['name']

    # Generate Pinyin Initials
    pinyin_initials = get_pinyin_initials(showname)
    
    # Decide whether to write initials based on settings
    if pinyin_initials:
        if SOURCE_SETTINGS.get("WRITE_INITIALS", True):
            sort_title = "{}|{}".format(pinyin_initials, showname)
            vtag.setSortTitle(sort_title)

        if SOURCE_SETTINGS.get("WRITE_INITIALS_ORIGINALTITLE", True):
            if original_name:
                original_name = "{}|{}|{}".format(pinyin_initials, showname, original_name)
            else:
                original_name = "{}|{}".format(pinyin_initials, showname)

    plot = _clean_plot(safe_get(show_info, 'overview', ''))
    vtag.setTitle(showname)
    vtag.setOriginalTitle(original_name)
    vtag.setTvShowTitle(showname)
    vtag.setPlot(plot)
    vtag.setPlotOutline(plot)
    vtag.setMediaType('tvshow')
    ext_ids = {'tmdb_id': show_info['id']}
    ext_ids.update(show_info.get('external_ids', {}))
    epguide_ids = _set_unique_ids(ext_ids, vtag)
    vtag.setEpisodeGuide(json.dumps(epguide_ids))
    if show_info.get('first_air_date'):
        vtag.setYear(int(show_info['first_air_date'][:4]))
        vtag.setPremiered(show_info['first_air_date'])
    if full_info:
        vtag.setTvShowStatus(safe_get(show_info, 'status', ''))
        vtag.setGenres(_get_names(show_info.get('genres', [])))
        
        # 处理 Tags (关键词 + 国家)
        tags = []
        if SOURCE_SETTINGS["SAVETAGS"]:
            tags.extend(_get_names(show_info.get('keywords', {}).get('results', [])))
        
        # 将 origin_country 转换为英文全名标签 (与筛选器 library.py 保持一致)
        origin_countries = show_info.get('origin_country', [])
        # 国家代码映射表 (ISO 3166-1 alpha-2 -> English Full Name)
        # 参考 library.py 中的映射逻辑
        COUNTRY_MAP = {
            'CN': 'China',          # 内地
            'HK': 'Hong Kong',      # 中国香港
            'TW': 'Taiwan',         # 中国台湾
            'US': 'United States',  # 美国
            'JP': 'Japan',          # 日本
            'KR': 'South Korea',    # 韩国
            'TH': 'Thailand',       # 泰国
            'IN': 'India',          # 印度
            'GB': 'United Kingdom', # 英国
            'FR': 'France',         # 法国
            'DE': 'Germany',        # 德国
            'RU': 'Russia',         # 俄罗斯
            'CA': 'Canada',         # 加拿大
            # 补充常见国家
            'AU': 'Australia',      # 澳大利亚
            'IT': 'Italy',          # 意大利
            'ES': 'Spain',          # 西班牙
            'BR': 'Brazil',         # 巴西
            'MX': 'Mexico',         # 墨西哥
            'SE': 'Sweden',         # 瑞典
            'NO': 'Norway',         # 挪威
            'DK': 'Denmark',        # 丹麦
            'NL': 'Netherlands',    # 荷兰
            'PL': 'Poland',         # 波兰
            'TR': 'Turkey',         # 土耳其
            'ID': 'Indonesia',      # 印度尼西亚
            'PH': 'Philippines',    # 菲律宾
            'SG': 'Singapore',      # 新加坡
            'MY': 'Malaysia',       # 马来西亚
            'VN': 'Vietnam',        # 越南
            'ZA': 'South Africa',   # 南非
            'NZ': 'New Zealand',    # 新西兰
            'IE': 'Ireland',        # 爱尔兰
            'BE': 'Belgium',        # 比利时
            'CH': 'Switzerland',    # 瑞士
            'AT': 'Austria',        # 奥地利
            'FI': 'Finland',        # 芬兰
            'PT': 'Portugal',       # 葡萄牙
            'GR': 'Greece',         # 希腊
            'IL': 'Israel',         # 以色列
            'AR': 'Argentina',      # 阿根廷
            'CL': 'Chile',          # 智利
            'CO': 'Colombia',       # 哥伦比亚
            'UA': 'Ukraine',        # 乌克兰
            'CZ': 'Czech Republic', # 捷克
            'HU': 'Hungary',        # 匈牙利
            'RO': 'Romania',        # 罗马尼亚
            # 更多国家补充
            'AF': 'Afghanistan', 'AL': 'Albania', 'DZ': 'Algeria', 'AD': 'Andorra', 'AO': 'Angola',
            'AG': 'Antigua and Barbuda', 'AM': 'Armenia', 'AZ': 'Azerbaijan', 'BS': 'Bahamas', 'BH': 'Bahrain',
            'BD': 'Bangladesh', 'BB': 'Barbados', 'BY': 'Belarus', 'BZ': 'Belize', 'BJ': 'Benin',
            'BT': 'Bhutan', 'BO': 'Bolivia', 'BA': 'Bosnia and Herzegovina', 'BW': 'Botswana', 'BN': 'Brunei Darussalam',
            'BG': 'Bulgaria', 'BF': 'Burkina Faso', 'BI': 'Burundi', 'KH': 'Cambodia', 'CM': 'Cameroon',
            'CV': 'Cape Verde', 'CF': 'Central African Republic', 'TD': 'Chad', 'KM': 'Comoros', 'CG': 'Congo',
            'CD': 'Democratic Republic of the Congo', 'CR': 'Costa Rica', 'CI': 'Cote D\'Ivoire', 'HR': 'Croatia', 'CU': 'Cuba',
            'CY': 'Cyprus', 'DJ': 'Djibouti', 'DM': 'Dominica', 'DO': 'Dominican Republic', 'EC': 'Ecuador',
            'EG': 'Egypt', 'SV': 'El Salvador', 'GQ': 'Equatorial Guinea', 'ER': 'Eritrea', 'EE': 'Estonia',
            'ET': 'Ethiopia', 'FJ': 'Fiji', 'GA': 'Gabon', 'GM': 'Gambia', 'GE': 'Georgia',
            'GH': 'Ghana', 'GD': 'Grenada', 'GT': 'Guatemala', 'GN': 'Guinea', 'GW': 'Guinea-Bissau',
            'GY': 'Guyana', 'HT': 'Haiti', 'HN': 'Honduras', 'IS': 'Iceland', 'IR': 'Iran',
            'IQ': 'Iraq', 'JM': 'Jamaica', 'JO': 'Jordan', 'KZ': 'Kazakhstan', 'KE': 'Kenya',
            'KI': 'Kiribati', 'KP': 'North Korea', 'KW': 'Kuwait', 'KG': 'Kyrgyzstan', 'LA': 'Laos',
            'LV': 'Latvia', 'LB': 'Lebanon', 'LS': 'Lesotho', 'LR': 'Liberia', 'LY': 'Libya',
            'LI': 'Liechtenstein', 'LT': 'Lithuania', 'LU': 'Luxembourg', 'MK': 'Macedonia', 'MG': 'Madagascar',
            'MW': 'Malawi', 'MV': 'Maldives', 'ML': 'Mali', 'MT': 'Malta', 'MH': 'Marshall Islands',
            'MR': 'Mauritania', 'MU': 'Mauritius', 'FM': 'Micronesia', 'MD': 'Moldova', 'MC': 'Monaco',
            'MN': 'Mongolia', 'ME': 'Montenegro', 'MA': 'Morocco', 'MZ': 'Mozambique', 'MM': 'Myanmar',
            'NA': 'Namibia', 'NR': 'Nauru', 'NP': 'Nepal', 'NI': 'Nicaragua', 'NE': 'Niger',
            'NG': 'Nigeria', 'OM': 'Oman', 'PK': 'Pakistan', 'PW': 'Palau', 'PA': 'Panama',
            'PG': 'Papua New Guinea', 'PY': 'Paraguay', 'PE': 'Peru', 'QA': 'Qatar', 'RW': 'Rwanda',
            'KN': 'Saint Kitts and Nevis', 'LC': 'Saint Lucia', 'VC': 'Saint Vincent and the Grenadines', 'WS': 'Samoa', 'SM': 'San Marino',
            'ST': 'Sao Tome and Principe', 'SA': 'Saudi Arabia', 'SN': 'Senegal', 'RS': 'Serbia', 'SC': 'Seychelles',
            'SL': 'Sierra Leone', 'SK': 'Slovakia', 'SI': 'Slovenia', 'SB': 'Solomon Islands', 'SO': 'Somalia',
            'LK': 'Sri Lanka', 'SD': 'Sudan', 'SR': 'Suriname', 'SZ': 'Swaziland', 'SY': 'Syria',
            'TJ': 'Tajikistan', 'TZ': 'Tanzania', 'TL': 'Timor-Leste', 'TG': 'Togo', 'TO': 'Tonga',
            'TT': 'Trinidad and Tobago', 'TN': 'Tunisia', 'TM': 'Turkmenistan', 'TV': 'Tuvalu', 'UG': 'Uganda',
            'AE': 'United Arab Emirates', 'UY': 'Uruguay', 'UZ': 'Uzbekistan', 'VU': 'Vanuatu', 'VE': 'Venezuela',
            'YE': 'Yemen', 'ZM': 'Zambia', 'ZW': 'Zimbabwe'
        }
        for code in origin_countries:
            if code in COUNTRY_MAP:
                tags.append(COUNTRY_MAP[code])
            # else: tags.append(code) # 可选：保留未映射的代码
            
        if tags:
            vtag.setTags(tags)

        networks = show_info.get('networks', [])
        if networks:
            network = networks[0]
            country = network.get('origin_country', '')
        else:
            network = None
            country = None
        if network and country and SOURCE_SETTINGS["STUDIOCOUNTRY"]:
            vtag.setStudios(['{0} ({1})'.format(network['name'], country)])
        elif network:
            vtag.setStudios([network['name']])
        if country:
            vtag.setCountries([country])
        content_ratings = show_info.get(
            'content_ratings', {}).get('results', {})
        if content_ratings:
            mpaa = ''
            mpaa_backup = ''
            for content_rating in content_ratings:
                iso = content_rating.get('iso_3166_1', '').lower()
                if iso == 'us':
                    mpaa_backup = content_rating.get('rating')
                if iso == SOURCE_SETTINGS["CERT_COUNTRY"].lower():
                    mpaa = content_rating.get('rating', '')
            if not mpaa:
                mpaa = mpaa_backup
            if mpaa:
                vtag.setMpaa(SOURCE_SETTINGS["CERT_PREFIX"] + mpaa)
        vtag.setWriters(_get_credits(show_info))
        if SOURCE_SETTINGS["ENABTRAILER"]:
            trailer = _parse_trailer(show_info.get(
                'videos', {}).get('results', {}))
            if trailer:
                vtag.setTrailer(trailer)
        list_item = set_show_artwork(show_info, list_item)
        _add_season_info(show_info, vtag)
        _set_cast(show_info['credits']['cast'], vtag)
        _set_rating(show_info, vtag)
    else:
        image = show_info.get('poster_path', '')
        if image and not image.endswith('.svg'):
            theurl = imagerooturl + image
            previewurl = previewrooturl + image
            vtag.addAvailableArtwork(
                theurl, arttype='poster', preview=previewurl)
    logger.debug('adding tv show information for %s to list item' % showname)
    return list_item


def add_episode_info(list_item, episode_info, full_info=True):
    # type: (ListItem, InfoType, bool) -> ListItem
    """Add episode info to a list item"""
    title = episode_info.get('name', 'Episode ' +
                             str(episode_info['episode_number']))
    vtag = list_item.getVideoInfoTag()
    vtag.setTitle(title)
    vtag.setSeason(episode_info['season_number'])
    vtag.setEpisode(episode_info['episode_number'])
    vtag.setMediaType('episode')
    if safe_get(episode_info, 'air_date') is not None:
        vtag.setFirstAired(episode_info['air_date'])
    if full_info:
        summary = safe_get(episode_info, 'overview')
        if summary is not None:
            plot = _clean_plot(summary)
            vtag.setPlot(plot)
            vtag.setPlotOutline(plot)
        if safe_get(episode_info, 'air_date') is not None:
            vtag.setPremiered(episode_info['air_date'])
        duration = episode_info.get('runtime')
        if duration:
            videostream = VideoStreamDetail(duration=int(duration)*60)
            vtag.addVideoStream(videostream)
        _set_cast(
            episode_info['season_cast'] + episode_info['credits']['guest_stars'], vtag)
        ext_ids = {'tmdb_id': episode_info['id']}
        ext_ids.update(episode_info.get('external_ids', {}))
        _set_unique_ids(ext_ids, vtag)
        _set_rating(episode_info, vtag)
        for image in episode_info.get('images', {}).get('stills', []):
            theurl, previewurl = get_image_urls(image)
            if theurl:
                vtag.addAvailableArtwork(
                    theurl, arttype='thumb', preview=previewurl)
        vtag.setWriters(_get_credits(episode_info))
        vtag.setDirectors(_get_directors(episode_info))
    logger.debug('adding episode information for S%sE%s - %s to list item' %
                 (episode_info['season_number'], episode_info['episode_number'], title))
    return list_item


def parse_nfo_url(nfo):
    # type: (Text) -> Optional[UrlParseResult]
    """Extract show ID and named seasons from NFO file contents"""
    # work around for xbmcgui.ListItem.addSeason overwriting named seasons from NFO files
    ns_regex = r'<namedseason number="(.*)">(.*)</namedseason>'
    ns_match = re.findall(ns_regex, nfo, re.I)
    sid_match = None
    ep_grouping = None
    for regexp in SHOW_ID_REGEXPS:
        logger.debug('trying regex to match service from parsing nfo:')
        logger.debug(regexp)
        show_id_match = re.search(regexp, nfo, re.I)
        if show_id_match:
            logger.debug('match group 1: ' + show_id_match.group(1))
            logger.debug('match group 2: ' + show_id_match.group(2))
            if show_id_match.group(1) == "themoviedb" or show_id_match.group(1) == "tmdb":
                try:
                    ep_grouping = show_id_match.group(3)
                except IndexError:
                    pass
                tmdb_id = show_id_match.group(2)
            else:
                tmdb_id = _convert_ext_id(
                    show_id_match.group(1), show_id_match.group(2))
            if tmdb_id:
                logger.debug('match group 3: ' + str(ep_grouping))
                sid_match = UrlParseResult('tmdb', tmdb_id, ep_grouping)
                break
    return sid_match, ns_match


def _convert_ext_id(ext_provider, ext_id):
    # type: (Text, Text) -> Text
    """get a TMDb ID from an external ID"""
    providers_dict = {'imdb': 'imdb_id',
                      'thetvdb': 'tvdb_id',
                      'tvdb': 'tvdb_id'}
    show_url = FIND_URL.format(ext_id)
    params = {'api_key': settings.TMDB_CLOWNCAR,
              'language': SOURCE_SETTINGS["LANG_DETAILS"]}
    provider = providers_dict.get(ext_provider)
    if provider:
        params['external_source'] = provider
        show_info = api_utils.load_info(show_url, params=params)
    else:
        show_info = None
    if show_info:
        tv_results = show_info.get('tv_results')
        if tv_results:
            return tv_results[0].get('id')
    return None


def parse_media_id(title):
    # type: (Text) -> Dict
    """get the ID from a title and return with the type"""
    title = title.lower()
    if title.startswith('tt') and title[2:].isdigit():
        # IMDB ID works alone because it is clear
        return {'type': 'imdb_id', 'title': title}
    # IMDB ID with prefix to match
    elif title.startswith('imdb/tt') and title[7:].isdigit():
        # IMDB ID works alone because it is clear
        return {'type': 'imdb_id', 'title': title[5:]}
    elif title.startswith('tmdb/') and title[5:].isdigit():  # TMDB ID
        return {'type': 'tmdb_id', 'title': title[5:]}
    elif title.startswith('tvdb/') and title[5:].isdigit():  # TVDB ID
        return {'type': 'tvdb_id', 'title': title[5:]}
    return None


def _parse_trailer(results):
    # type: (Text) -> Text
    """create a valid Tubed or YouTube plugin trailer URL"""
    if results:
        if SOURCE_SETTINGS["PLAYERSOPT"] == 'tubed':
            addon_player = 'plugin://plugin.video.tubed/?mode=play&video_id='
        elif SOURCE_SETTINGS["PLAYERSOPT"] == 'youtube':
            addon_player = 'plugin://plugin.video.youtube/play/?video_id='
        backup_keys = []
        for video_lang in [SOURCE_SETTINGS["LANG_DETAILS"][0:2], 'en']:
            for result in results:
                if result.get('site') == 'YouTube' and result.get('iso_639_1') == video_lang:
                    key = result.get('key')
                    if result.get('type') == 'Trailer':
                        if _check_youtube(key):
                            # video is available and is defined as "Trailer" by TMDB. Perfect link!
                            return addon_player+key
                    else:
                        # video is available, but NOT defined as "Trailer" by TMDB. Saving it as backup in case it doesn't find any perfect link.
                        backup_keys.append(key)
            for keybackup in backup_keys:
                if _check_youtube(keybackup):
                    return addon_player+keybackup
    return None


def _check_youtube(key):
    # type: (Text) -> bool
    """check to see if the YouTube key returns a valid link"""
    chk_link = "https://www.youtube.com/watch?v="+key
    check = api_utils.load_info(chk_link, resp_type='not_json')
    if not check or "Video unavailable" in check:       # video not available
        return False
    return True
