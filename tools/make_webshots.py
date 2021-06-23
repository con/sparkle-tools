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
import subprocess
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
    sha = rec['sparkle-build-sha1'] = e.get_attribute('content')

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
    rec['participants'] = n

    if sha and os.environ.get("SPARKLE_CODEBASE"):
        r = subprocess.run(
            ['git', '-C', os.environ["SPARKLE_CODEBASE"], 'describe', '--all', sha],
            stdout=subprocess.PIPE,
            universal_newlines=True,
        )
        if not r.returncode:  # only if didn't fail and found it
            rec['sparkle-build-describe'] = r.stdout.rstrip()
    return rec


def case_poster_and_back(driver):
    """requires login to run first and us being on the home page"""
    ts = {}
    rec = {
        'times': ts,
    }
    modal = driver.find_element_by_xpath('//img[@alt="Poster Hall"]')

    timer = Timer()
    modal.click()

    def wait_class(cls, name=None):
        name = name or cls
        e = wait_until(
            driver,
            EC.presence_of_element_located((By.CLASS_NAME, cls))
        )
        if name in ts:
            # add some index
            for idx in range(100):
                n = f"{name}#{idx}"
                if n in ts:
                    continue
                name = n
                break
        ts[name] = timer()
        return e

    wait_class('room-entry-button').click()

    wait_class('PosterHallSearch__input')

    wait_class('PosterHall__more-button')

    # choose first poster listed
    wait_class("PosterPreview").click()

    more_info = wait_class('PosterPage__moreInfoUrl')
    rec['visited_poster'] = more_info.text

    driver.find_element_by_xpath('//span[@class="back-link"]').click()

    wait_class('PosterHallSearch__input')

    # go to the likely the same poster but then home from there
    wait_class("PosterPreview").click()
    more_info = wait_class('PosterPage__moreInfoUrl')
    rec['visited_poster#2'] = more_info.text

    driver.find_element_by_xpath('//*[@data-icon="home"]').click()

    wait_class('maproom')
    return rec


def wait_until(driver, until):
    return WebDriverWait(driver, 300, poll_frequency=0.1).until(until)


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
        url = sys.argv[1]
    else:
        url = 'https://ohbm.sparkle.space'
    url = url.rstrip('/')  # for consistency

    # To guarantee that we time out if something gets stuck
    socket.setdefaulttimeout(300)
    overall = Timer()
    driver = get_ready_driver()
    try:
        allstats = {}

        # yoh recommends to create a file with those secrets exported outside of the repo
        allstats['initial-login'] = login(driver, url, os.environ["SPARKLE_USERNAME"], os.environ["SPARKLE_PASSWORD"])
        allstats['poster-and-back'] = case_poster_and_back(driver)

        allstats['total'] = overall()
        print(json.dumps(allstats, indent=2))
    finally:
        driver.quit()

