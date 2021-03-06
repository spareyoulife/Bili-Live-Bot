import copy
import asyncio
import hashlib
from itertools import count
from typing import Callable, Optional

import printer
import conf_loader
import exceptions
from web_session import WebSession
from tasks.login import LoginTask


class User:
    _ids = count(0)
    __slots__ = (
        'id', 'force_sleep', 'name', 'password', 'manage_room', 'alerts', 'gift_comb_delay', 'alert_second', 'gift_thx_format', 'focus_thx_format', 'alias', 'task_ctrl',
        'danmu_length', 'random_list_1', 'random_list_2', 'random_list_3',
        'medal_update_format', 'medal_update_check_delay',
        'task_arrangement', 'is_in_jail',
        'guard_thx_format', 'fans_check_delay', 'only_live_thx',
        'silver_gift_thx_format', 'gold_gift_thx_format',
        'reply', 'ban',
        'height', 'weight',

        'bililive_session', 'login_session', 'other_session',

        'dict_bili', 'app_params', 'repost_del_lock',
        'dyn_lottery_friends', 'storm_lock',
        '_waiting_login', '_loop'
    )

    def __init__(
            self, dict_user: dict, task_ctrl: dict, task_arrangement: dict, dict_bili: dict, force_sleep: Callable):
        self.id = next(self._ids)
        self.force_sleep = force_sleep
        self.name = dict_user['username']
        self.password = dict_user['password']
        self.alias = dict_user.get('alias', self.name)
        self.manage_room = dict_user['manage_room']
        self.alerts = dict_user.get('alerts', [])
        self.gift_comb_delay = dict_user['gift_comb_delay']
        self.alert_second = dict_user['alert_second']
        self.gift_thx_format = dict_user.get('gift_thx_format', '感谢{username}投喂的{giftname}x{num}')
        self.silver_gift_thx_format = dict_user.get('silver_gift_thx_format', self.gift_thx_format)
        self.gold_gift_thx_format = dict_user.get('gold_gift_thx_format', self.gift_thx_format)
        self.focus_thx_format = dict_user['focus_thx_format']
        self.guard_thx_format = dict_user.get('guard_thx_format', self.gift_thx_format)
        self.danmu_length = dict_user.get('danmu_length', 30)
        self.medal_update_format = dict_user.get('medal_update_format', '')
        self.medal_update_check_delay = dict_user.get('medal_update_check_delay', 30)
        self.only_live_thx = dict_user.get('only_live_thx', False)
        self.reply = dict_user.get('reply', [])
        self.ban = dict_user.get('ban', [])
        self.height = dict_user.get('height', 0)
        self.weight = dict_user.get('weight', 0)

        self.fans_check_delay = dict_user.get('fans_check_delay', 20)
        

        self.random_list_1 = dict_user.get('random_list_1', [])
        self.random_list_2 = dict_user.get('random_list_2', [])
        self.random_list_3 = dict_user.get('random_list_3', [])
        if len(self.random_list_1) == 0:
            self.random_list_1 = [""]
        if len(self.random_list_2) == 0:
            self.random_list_2 = [""]
        if len(self.random_list_3) == 0:
            self.random_list_3 = [""]

        self.task_ctrl = task_ctrl
        self.task_arrangement = task_arrangement
        self.is_in_jail = False  # 是否小黑屋

        self.bililive_session = WebSession()
        self.login_session = WebSession()
        self.other_session = WebSession()

        # 每个user里面都分享了同一个dict，必须要隔离，否则更新cookie这些的时候会互相覆盖
        self.dict_bili = copy.deepcopy(dict_bili)
        self.app_params = {
            'actionKey': dict_bili['actionKey'],
            'appkey': dict_bili['appkey'],
            'build': dict_bili['build'],
            'device': dict_bili['device'],
            'mobi_app': dict_bili['mobi_app'],
            'platform': dict_bili['platform'],
        }
        self.update_login_data(dict_user)

        self._waiting_login = None
        self._loop = asyncio.get_event_loop()

        self.repost_del_lock = asyncio.Lock()  # 在follow与unfollow过程中必须保证安全(repost和del整个过程加锁)
        dyn_lottery_friends = [(str(uid), name)
                               for uid, name in task_ctrl['dyn_lottery_friends'].items()]
        self.dyn_lottery_friends = dyn_lottery_friends  # list (uid, name)
        self.storm_lock = asyncio.Semaphore(1)  # 用于控制同时进行的风暴数目(注意是单个用户的)

    def update_login_data(self, login_data):
        for i, value in login_data.items():
            self.dict_bili[i] = value
            if i == 'cookie':
                self.dict_bili['pcheaders']['cookie'] = value
                self.dict_bili['appheaders']['cookie'] = value
        conf_loader.write_user(login_data, self.id)

    def update_log(self):
        conf_loader.write_user({'weight': self.weight, 'height': self.height}, self.id)

    def is_online(self):
        return self.dict_bili['pcheaders']['cookie'] and self.dict_bili['appheaders']['cookie']

    def info(
            self,
            *objects,
            with_userid=True,
            **kwargs):
        if with_userid:
            printer.info(
                *objects,
                **kwargs,
                extra_info=f'用户id:{self.id} 名字:{self.alias}')
        else:
            printer.info(*objects, **kwargs)

    def warn(self, *objects, **kwargs):
        printer.warn(
            *objects,
            **kwargs,
            extra_info=f'用户id:{self.id} 名字:{self.alias}')

    def sort_and_sign(self, extra_params: Optional[dict] = None) -> dict:
        if extra_params is None:
            dict_params = self.app_params.copy()
        else:
            dict_params = {**self.app_params, **extra_params}

        list_params = [f'{key}={value}' for key, value in dict_params.items()]
        list_params.sort()
        text = "&".join(list_params)
        text_with_appsecret = f'{text}{self.dict_bili["app_secret"]}'
        sign = hashlib.md5(text_with_appsecret.encode('utf-8')).hexdigest()
        dict_params['sign'] = sign
        return dict_params

    async def req_s(self, func, *args, timeout=None):
        while True:
            if self._waiting_login is None:
                try:
                    return await asyncio.wait_for(func(*args), timeout=timeout)
                except asyncio.TimeoutError:
                    self.info(f'TASK {func} 请求超时，即将 CANCEL')
                    raise asyncio.CancelledError()
                except exceptions.LogoutError:  # logout
                    if self._waiting_login is None:  # 当前没有处理的运行
                        self.info('判定出现了登陆失败，且未处理')
                        self._waiting_login = self._loop.create_future()
                        try:
                            await LoginTask.handle_login_status(self)
                            self.info('已经登陆了')
                        except asyncio.CancelledError:  # 登陆中取消，把waiting_login设置，否则以后的req会一直堵塞
                            raise
                        finally:
                            self._waiting_login.set_result(-1)
                            self._waiting_login = None
                    else:  # 已有处理的运行了
                        self.info('判定出现了登陆失败，已经处理')
                        await self._waiting_login
                except exceptions.ForbiddenError:
                    await asyncio.shield(self.force_sleep(3600))  # bili_sched.force_sleep
                    await asyncio.sleep(3600)  # 有的function不受sched控制，主动sleep即可，不cancel原因是怕堵死一些协程
            else:
                await self._waiting_login

    def fall_in_jail(self):
        self.is_in_jail = True
        self.info(f'用户进入小黑屋')

    def out_of_jail(self):
        self.is_in_jail = False
        self.info(f'抽奖脚本尝试性设置用户已出小黑屋（如果实际没出还会再判定进去）')

    def print_status(self):
        jail_status = '恭喜中奖' if self.is_in_jail else '自由之身'
        self.info('当前用户的状态：', jail_status)
