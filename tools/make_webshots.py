#!/usr/bin/env python3
from collections import defaultdict
from dataclasses import dataclass
from functools import partial
import logging
from operator import attrgetter
import os
from pathlib import Path
import socket
import statistics
import sys
import time
from typing import List, Optional, Tuple, Union

from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.wait import WebDriverWait
from selenium.common.exceptions import WebDriverException
import yaml
import json

log = logging.getLogger(__name__)

PAGES = ["landing", "edit-metadata", "view-data"]

# might come handy, not used ATM
@dataclass
class LoadStat:
    dandiset: str
    page: str
    time: Union[float, str]
    label: str
    url: Optional[str]

    def get_columns(self) -> Tuple[str, str]:
        t = self.time if isinstance(self.time, str) else f"{self.time:.2f}"
        header = f"t={t}"
        if self.url is not None:
            header += f" [{self.label}]({self.url})"
        else:
            header += f" {self.label}"
        cell = f"![]({self.dandiset}/{self.page}.png)"
        return (header, cell)

    def has_time(self) -> bool:
        return isinstance(self.time, float)

# might come handy, not used ATM
def render_stats(dandiset: str, stats: List[LoadStat]) -> str:
    s = f"### {dandiset}\n\n"
    header, row = zip(*map(LoadStat.get_columns, stats))
    s += "| " + " | ".join(header) + " |\n"
    s += "| --- " * len(stats) + "|\n"
    s += "| " + " | ".join(row) + " |\n"
    s += "\n"
    return s


class Timer:
    def __init__(self):
        self.t = self.t0 = time.time()

    def __call__(self):
        t = time.time()
        dt = t - self.t
        self.t = t
        return dt


def login(driver, url, username, password):
    ts = {}
    rec = {
        'times': ts,
    }
    timer = Timer()

    driver.get(url + '/in/home')
    btn = wait_until(
        driver,
        EC.presence_of_element_located((By.CLASS_NAME, 'login-button'))
    )
    btn.click()
    # driver.find_element_by_class_name('login-button').click()
    email = wait_until(driver, EC.presence_of_element_located((By.XPATH, '//input[@name="email"]')))
    email.send_keys(username)

    pwd = wait_until(driver, EC.presence_of_element_located((By.XPATH, '//input[@name="password"]')))
    pwd.send_keys(password)

    login = wait_until(driver, EC.presence_of_element_located((By.XPATH, '//input[@value="Log in"]')))
    login.click()

    ts['login'] = timer()

    # wait for the nav-sparkle-logo div to appear as a signal that we went through the
    # blue screen of sparkle
    wait_until(driver, EC.presence_of_element_located((By.CLASS_NAME, 'nav-sparkle-logo')))
    ts['main-screen-appear'] = timer()

    # from now on even finding an element becomes a "heavy task"
    # get info on what state we use
    e = driver.find_element_by_xpath('/html/head/meta[@name="sparkle-build-sha1"]')
    rec['sparkle-build-sha1'] = e.get_attribute('content')

    # silly way to wait until we see some reasonable number of attendees
    for i in range(100):
        # might need to wait_until!?
        e = driver.find_element_by_xpath('//div[@class="venue-partygoers-container"]')
        if e:
            n = e.text.split()
            if n:
                n = int(n[0])
                if n > 100:
                    break
                print(f"Detected only {n} participants, waiting longer")
        time.sleep(0.1)
    else:
        raise RuntimeError("Did not get reasonable number of participants.")
    ts['participants-appear'] = timer()
    return rec


def wait_no_progressbar(driver, cls):
    WebDriverWait(driver, 300, poll_frequency=0.1).until(
        EC.invisibility_of_element_located((By.CLASS_NAME, cls)))


def wait_until(driver, until):
    return WebDriverWait(driver, 300, poll_frequency=0.1).until(until)


def __process_dandiset(driver, ds):

    def click_edit():
        # might still take a bit to appear
        # TODO: more sensible way to "identify" it: https://github.com/dandi/dandiarchive/issues/648
        edit_button = WebDriverWait(driver, 3).until(
            EC.element_to_be_clickable(
                (By.XPATH,
                 '//button[@id="view-edit-metadata"]'
                 )))
        edit_button.click()

    dspath = Path(ds)
    if not dspath.exists():
        dspath.mkdir(parents=True)

    info = {'times': {}}
    times = info['times']
    stats = []

    # TODO: do not do draft unless there is one
    # TODO: do for a released version
    for urlsuf, page, wait, act in [
        ('', 'landing', partial(wait_no_progressbar, driver, "v-progress-circular"), None),
        (None, 'edit-metadata', partial(wait_no_progressbar, driver, "v-progress-circular"), click_edit),
        ('/draft/files', 'view-data', partial(wait_no_progressbar, driver, "v-progress-linear"), None)]:

        log.info(f"{ds} {page}")
        page_name = dspath / page
        # So we could try a few times in case of catching WebDriverException
        # e.g. as in the case of "invalid session id" whenever we would reinitialize
        # the entire driver
        for trial in range(3):
            page_name.with_suffix('.png').unlink(missing_ok=True)
            t0 = time.monotonic()
            # ad-hoc workaround for https://github.com/dandi/dandiarchive/issues/662
            # with hope it is the only one and to not overcomplicate things
            # so if we fail, we do not carry outdated one
            #if ds in ('000040', '000041') and page == 'edit-metadata':
            #    t = "timeout/crash"
            #    break
            try:
                if urlsuf is not None:
                    log.debug("Before get")
                    driver.get(f'{ARCHIVE_GUI}/#/dandiset/{ds}{urlsuf}')
                    log.debug("After get")
                if act:
                    log.debug("Before act")
                    act()
                    log.debug("After act")
                if wait:
                    log.debug("Before wait")
                    wait()
                    log.debug("After wait")
            except TimeoutException:
                log.debug("Timed out")
                t = 'timeout'
                break
            except WebDriverException as exc:
                # do not bother trying to resurrect - it seems to not working really based on
                # 000040 timeout experience
                raise
                t = str(exc).rstrip()  # so even if we continue out of the loop
                log.warning(f"Caught {exc}. Reinitializing")
                # it might be a reason for subsequent "Max retries exceeded"
                # since it closes "too much"
                #try:
                #    driver.quit()  # cleanup if still can
                #finally:
                driver = get_ready_driver()
                continue
            except Exception as exc:
                log.warning(f"Caught unexpected {exc}.")
                t = str(exc).rstrip()
                break
            else:
                t = time.monotonic() - t0
                time.sleep(2)  # to overcome https://github.com/dandi/dandiarchive/issues/650 - animations etc
                driver.save_screenshot(str(page_name.with_suffix('.png')))
                break
        times[page] = t
        stats.append(LoadStat(
            dandiset=ds,
            page=page,
            time=t,
            label='Edit Metadata' if page == 'edit-metadata' else 'Go to page',
            url=f'{ARCHIVE_GUI}/#/dandiset/{ds}{urlsuf}' if urlsuf is not None else None,
        ))
        # now that we do login, do not bother storing html to not leak anything sensitive by mistake
        # page_name.with_suffix('.html').write_text(driver.page_source)

    with (dspath / 'info.yaml').open('w') as f:
        yaml.safe_dump(info, f)
    return stats


# to help with "invalid session id" by reinitializing the entire driver
def get_ready_driver():
    options = Options()
    if False:  # interactive_logged_in:
        options.add_argument('--new-window')
    else:
        options.add_argument('--no-sandbox')
        # options.add_argument('--headless')
        options.add_argument('--incognito')
        # options.add_argument('--disable-gpu')
        options.add_argument('--disable-dev-shm-usage')
    options.add_argument("--window-size=1024,1400")
    #driver.set_page_load_timeout(30)
    #driver.set_script_timeout(30)
    #driver.implicitly_wait(10)
    driver = webdriver.Chrome(options=options)
    return driver


if __name__ == '__main__':
    logging.basicConfig(
        format="%(asctime)s [%(levelname)-8s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
        level=logging.INFO,
    )

    if len(sys.argv) > 1:
        url = sys.argv[1:]
    else:
        url = 'https://ohbm.sparkle.space/'

    # To guarantee that we time out if something gets stuck
    socket.setdefaulttimeout(300)
    driver = get_ready_driver()
    allstats = []
    # yoh recommends to create a file with those secrets exported outside of the repo
    rec = login(driver, url, os.environ["SPARKLE_USERNAME"], os.environ["SPARKLE_PASSWORD"])
    print(json.dumps(rec, indent=2))
    driver.quit()

