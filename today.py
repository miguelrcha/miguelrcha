import datetime
import hashlib
import os
import time

import requests
from dateutil import relativedelta
from lxml import etree

# Fine-grained personal access token with All Repositories access:
# Account permissions: read:Followers, read:Starring, read:Watching
# Repository permissions: read:Commit statuses, read:Contents, read:Issues, read:Metadata, read:Pull Requests
HEADERS = {'authorization': 'token ' + os.environ['ACCESS_TOKEN']}
USER_NAME = os.environ['USER_NAME']  # 'miguelrcha'
BIRTHDAY = datetime.datetime(2007, 8, 20)
QUERY_COUNT = {'user_getter': 0, 'follower_getter': 0, 'following_getter': 0, 'graph_repos_stars': 0, 'recursive_loc': 0, 'loc_query': 0}


def daily_readme(birthday):
    """
    Returns the length of time since `birthday`, e.g. 'XX years, XX months, XX days'
    """
    diff = relativedelta.relativedelta(datetime.datetime.today(), birthday)
    return '{} {}, {} {}, {} {}{}'.format(
        diff.years, 'year' + format_plural(diff.years),
        diff.months, 'month' + format_plural(diff.months),
        diff.days, 'day' + format_plural(diff.days),
        ' 🎂' if (diff.months == 0 and diff.days == 0) else '')


def format_plural(unit):
    return 's' if unit != 1 else ''


def simple_request(func_name, query, variables):
    """
    Returns a request, or raises an Exception if the response does not succeed.
    """
    request = requests.post('https://api.github.com/graphql', json={'query': query, 'variables': variables}, headers=HEADERS)
    if request.status_code == 200:
        return request
    raise Exception(func_name, ' has failed with a', request.status_code, request.text, QUERY_COUNT)


def graph_repos_stars(count_type, owner_affiliation, cursor=None):
    """
    Uses GitHub's GraphQL v4 API to return my total repository or star count.
    """
    query_count('graph_repos_stars')
    query = '''
    query ($owner_affiliation: [RepositoryAffiliation], $login: String!, $cursor: String) {
        user(login: $login) {
            repositories(first: 100, after: $cursor, ownerAffiliations: $owner_affiliation) {
                totalCount
                edges {
                    node {
                        ... on Repository {
                            nameWithOwner
                            stargazers {
                                totalCount
                            }
                        }
                    }
                }
                pageInfo {
                    endCursor
                    hasNextPage
                }
            }
        }
    }'''
    variables = {'owner_affiliation': owner_affiliation, 'login': USER_NAME, 'cursor': cursor}
    request = simple_request(graph_repos_stars.__name__, query, variables)
    if count_type == 'repos':
        return request.json()['data']['user']['repositories']['totalCount']
    elif count_type == 'stars':
        return stars_counter(request.json()['data']['user']['repositories']['edges'])


def recursive_loc(owner, repo_name, data, addition_total=0, deletion_total=0, my_commits=0, cursor=None):
    """
    Uses GitHub's GraphQL v4 API and cursor pagination to fetch 100 commits from a repository at a time
    """
    query_count('recursive_loc')
    query = '''
    query ($repo_name: String!, $owner: String!, $cursor: String) {
        repository(name: $repo_name, owner: $owner) {
            defaultBranchRef {
                target {
                    ... on Commit {
                        history(first: 100, after: $cursor) {
                            totalCount
                            edges {
                                node {
                                    ... on Commit {
                                        committedDate
                                    }
                                    author {
                                        user {
                                            id
                                        }
                                    }
                                    deletions
                                    additions
                                }
                            }
                            pageInfo {
                                endCursor
                                hasNextPage
                            }
                        }
                    }
                }
            }
        }
    }'''
    variables = {'repo_name': repo_name, 'owner': owner, 'cursor': cursor}
    request = requests.post('https://api.github.com/graphql', json={'query': query, 'variables': variables}, headers=HEADERS)  # can't use simple_request(), need to save the file before raising
    if request.status_code == 200:
        if request.json()['data']['repository']['defaultBranchRef'] is not None:  # only count commits if repo isn't empty
            return loc_counter_one_repo(request.json()['data']['repository']['defaultBranchRef']['target']['history'], addition_total, deletion_total, my_commits, owner, repo_name, data)
        else:
            return 0, 0, 0
    force_close_file(data)  # save whatever is in the file before the program crashes
    if request.status_code == 403:
        raise Exception('Too many requests in a short amount of time!\nYou\'ve hit the non-documented anti-abuse limit!')
    raise Exception('recursive_loc() has failed with a', request.status_code, request.text, QUERY_COUNT)


def loc_counter_one_repo(history, addition_total, deletion_total, my_commits, owner, repo_name, data):
    """
    Recursively call recursive_loc (GraphQL only returns 100 commits at a time)
    Only counts the LOC of commits authored by me
    """
    for node in history['edges']:
        author = node['node']['author']['user']
        if author is not None and author['id'] == OWNER_ID:
            my_commits += 1
            addition_total += node['node']['additions']
            deletion_total += node['node']['deletions']

    if history['edges'] == [] or not history['pageInfo']['hasNextPage']:
        return addition_total, deletion_total, my_commits
    return recursive_loc(owner, repo_name, data, addition_total, deletion_total, my_commits, history['pageInfo']['endCursor'])


def loc_query(owner_affiliation, cursor=None, edges=None):
    """
    Queries every repo I have access to (respecting owner_affiliation), 60 at a time
    (larger pages 502, smaller pages hit rate limits)
    """
    edges = edges or []
    query_count('loc_query')
    query = '''
    query ($owner_affiliation: [RepositoryAffiliation], $login: String!, $cursor: String) {
        user(login: $login) {
            repositories(first: 60, after: $cursor, ownerAffiliations: $owner_affiliation, isFork: false) {
                edges {
                    node {
                        ... on Repository {
                            nameWithOwner
                            defaultBranchRef {
                                target {
                                    ... on Commit {
                                        history {
                                            totalCount
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
                pageInfo {
                    endCursor
                    hasNextPage
                }
            }
        }
    }'''
    variables = {'owner_affiliation': owner_affiliation, 'login': USER_NAME, 'cursor': cursor}
    request = simple_request(loc_query.__name__, query, variables)
    repos = request.json()['data']['user']['repositories']
    if repos['pageInfo']['hasNextPage']:
        return loc_query(owner_affiliation, repos['pageInfo']['endCursor'], edges + repos['edges'])
    return cache_builder(edges + repos['edges'])


def cache_builder(edges, loc_add=0, loc_del=0, commits=0):
    """
    Checks each repository in edges against the cache; if a repo's commit count changed
    since the last run, re-walks its history with recursive_loc to refresh the LOC/commit counts.
    """
    filename = 'cache/' + hashlib.sha256(USER_NAME.encode('utf-8')).hexdigest() + '.txt'  # per-user cache file
    try:
        with open(filename, 'r') as f:
            data = f.readlines()
    except FileNotFoundError:
        data = []
        with open(filename, 'w') as f:
            f.writelines(data)

    if len(data) != len(edges):  # repo count changed: rebuild the cache skeleton
        flush_cache(edges, filename)
        with open(filename, 'r') as f:
            data = f.readlines()

    for index in range(len(edges)):
        repo_hash, commit_count = data[index].split()[:2]
        if repo_hash == hashlib.sha256(edges[index]['node']['nameWithOwner'].encode('utf-8')).hexdigest():
            try:
                if int(commit_count) != edges[index]['node']['defaultBranchRef']['target']['history']['totalCount']:
                    owner, repo_name = edges[index]['node']['nameWithOwner'].split('/')
                    add, dele, my_commits = recursive_loc(owner, repo_name, data)
                    data[index] = repo_hash + ' ' + str(edges[index]['node']['defaultBranchRef']['target']['history']['totalCount']) + ' ' + str(my_commits) + ' ' + str(add) + ' ' + str(dele) + '\n'
            except TypeError:  # empty repo
                data[index] = repo_hash + ' 0 0 0 0\n'
    with open(filename, 'w') as f:
        f.writelines(data)
    for line in data:
        my_commits, add, dele = line.split()[-3:]
        commits += int(my_commits)
        loc_add += int(add)
        loc_del += int(dele)
    return [loc_add, loc_del, loc_add - loc_del, commits]


def flush_cache(edges, filename):
    """
    Rebuilds the cache file skeleton (called when the number of repos changes, or on first run)
    """
    with open(filename, 'w') as f:
        for node in edges:
            f.write(hashlib.sha256(node['node']['nameWithOwner'].encode('utf-8')).hexdigest() + ' 0 0 0 0\n')


def force_close_file(data):
    """
    Saves whatever LOC data has been collected so far before the program crashes,
    so the next run doesn't have to start from scratch.
    """
    filename = 'cache/' + hashlib.sha256(USER_NAME.encode('utf-8')).hexdigest() + '.txt'
    with open(filename, 'w') as f:
        f.writelines(data)
    print('There was an error while writing to the cache file. The file,', filename, 'has had the partial data saved and closed.')


def stars_counter(data):
    total_stars = 0
    for node in data:
        total_stars += node['node']['stargazers']['totalCount']
    return total_stars


def svg_overwrite(filename, age_data, repo_data, star_data, follower_data, following_data, commit_data, loc_data):
    """
    Parses the profile card SVG as XML and updates the stat elements in place.
    """
    tree = etree.parse(filename)
    root = tree.getroot()
    justify_format(root, 'age_data', age_data, 40)
    justify_format(root, 'repo_data', repo_data, 6)
    justify_format(root, 'star_data', star_data, 11)
    justify_format(root, 'follower_data', follower_data, 9)
    justify_format(root, 'following_data', following_data, 8)
    justify_format(root, 'commit_data', commit_data, 11)
    justify_format(root, 'loc_data', loc_data[2], 9)
    justify_format(root, 'loc_add', loc_data[0])
    justify_format(root, 'loc_del', loc_data[1])
    tree.write(filename, encoding='utf-8', xml_declaration=True)


def justify_format(root, element_id, new_text, length=0):
    """
    Updates the text of `element_id` and pads the preceding `{element_id}_dots`
    element so the value stays right-aligned as its digit count changes.
    """
    if isinstance(new_text, int):
        new_text = '{:,}'.format(new_text)
    new_text = str(new_text)
    find_and_replace(root, element_id, new_text)
    just_len = max(0, length - len(new_text))
    if just_len <= 2:
        dot_string = {0: '', 1: ' ', 2: '. '}[just_len]
    else:
        dot_string = ' ' + ('.' * just_len) + ' '
    find_and_replace(root, f'{element_id}_dots', dot_string)


def find_and_replace(root, element_id, new_text):
    element = root.find(f".//*[@id='{element_id}']")
    if element is not None:
        element.text = new_text


def user_getter(username):
    """
    Returns the account id of the user (needed to attribute commits to them)
    """
    query_count('user_getter')
    query = '''
    query($login: String!){
        user(login: $login) {
            id
        }
    }'''
    request = simple_request(user_getter.__name__, query, {'login': username})
    return {'id': request.json()['data']['user']['id']}


def follower_getter(username):
    query_count('follower_getter')
    query = '''
    query($login: String!){
        user(login: $login) {
            followers {
                totalCount
            }
        }
    }'''
    request = simple_request(follower_getter.__name__, query, {'login': username})
    return int(request.json()['data']['user']['followers']['totalCount'])


def following_getter(username):
    query_count('following_getter')
    query = '''
    query($login: String!){
        user(login: $login) {
            following {
                totalCount
            }
        }
    }'''
    request = simple_request(following_getter.__name__, query, {'login': username})
    return int(request.json()['data']['user']['following']['totalCount'])


def query_count(funct_id):
    global QUERY_COUNT
    QUERY_COUNT[funct_id] += 1


def perf_counter(funct, *args):
    start = time.perf_counter()
    funct_return = funct(*args)
    return funct_return, time.perf_counter() - start


def formatter(query_type, difference):
    print('{:<23}'.format('   ' + query_type + ':'), sep='', end='')
    print('{:>12}'.format('%.4f' % difference + ' s ')) if difference > 1 else print('{:>12}'.format('%.4f' % (difference * 1000) + ' ms'))


if __name__ == '__main__':
    """
    Miguel Rocha (miguelrcha) — adapted from Andrew6rant/Andrew6rant
    """
    print('Calculation times:')
    user_data, user_time = perf_counter(user_getter, USER_NAME)
    OWNER_ID = user_data['id']
    formatter('account data', user_time)

    age_data, age_time = perf_counter(daily_readme, BIRTHDAY)
    formatter('age calculation', age_time)

    total_loc, loc_time = perf_counter(loc_query, ['OWNER', 'COLLABORATOR', 'ORGANIZATION_MEMBER'])
    formatter('LOC (cached)', loc_time)

    star_data, star_time = perf_counter(graph_repos_stars, 'stars', ['OWNER'])
    formatter('star counter', star_time)

    repo_data, repo_time = perf_counter(graph_repos_stars, 'repos', ['OWNER'])
    formatter('repo counter', repo_time)

    follower_data, follower_time = perf_counter(follower_getter, USER_NAME)
    formatter('follower counter', follower_time)

    following_data, following_time = perf_counter(following_getter, USER_NAME)
    formatter('following counter', following_time)

    svg_overwrite('card.svg', age_data, repo_data, star_data, follower_data, following_data, total_loc[3], total_loc)

    print('Total GitHub GraphQL API calls:', '{:>3}'.format(sum(QUERY_COUNT.values())))
    for funct_name, count in QUERY_COUNT.items():
        print('{:<28}'.format('   ' + funct_name + ':'), '{:>6}'.format(count))
