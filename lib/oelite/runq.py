from oebakery import die, err, warn, info, debug
from oelite import *
from oelite.dbutil import *
import oelite.recipe

import sys
import os
import copy

class OEliteRunQueue:


    def __init__(self, config, cookbook, rebuild=None, relax=None,
                 depth_first=True):
        self.cookbook = cookbook
        self.config = config
        # 1: --rebuild, 2: --rebuildall, 3: --reallyrebuildall
        self.rebuild = rebuild
        self.relax = relax
        self.depth_first = depth_first
        self._assume_provided = (self.config.getVar("ASSUME_PROVIDED", 1)
                                or "").split()
        self.runable = []
        self.metahashable = []
        self.cookbook.db.execute("ATTACH ':memory:' AS runq")
        self.dbc = self.cookbook.db.cursor()
        self.init_db()
        return


    def init_db(self):
        self.dbc.execute(
            "CREATE TABLE IF NOT EXISTS runq.provider ( "
            "type		TEXT, "
            "item		TEXT, "
            "package		INTEGER )")

        self.dbc.execute(
            "CREATE TABLE IF NOT EXISTS runq.task ( "
            "task		INTEGER, "
            "prime		INTEGER, "
            "build		INTEGER, "
            "relax		INTEGER, "
            "status		INTEGER, "
            "metahash		TEXT, "
            "mtime		REAL, "
            "tmphash		TEXT, "
            "buildhash		TEXT, "
            "UNIQUE (task) ON CONFLICT IGNORE )")

        self.dbc.execute(
            "CREATE TABLE IF NOT EXISTS runq.depend ( "
            "id			INTEGER PRIMARY KEY, "
            "task		INTEGER, " # references task.id
            "prime		INTEGER, " # boolean
            "parent_task	INTEGER, " # references task.id
            "depend_package	INTEGER DEFAULT -1, " # package
            "rdepend_package	INTEGER DEFAULT -1, " # package
            "filename		TEXT, "
            "prebake		INTEGER, "
            "UNIQUE (task, parent_task, depend_package, rdepend_package) "
            "ON CONFLICT IGNORE )")

        self.dbc.execute(
            "CREATE TABLE IF NOT EXISTS runq.recdepend ( "
            "package		INTEGER, "
            "parent_recipe	INTEGER, "
            "parent_package	INTEGER )")

        self.dbc.execute(
            "CREATE TABLE IF NOT EXISTS runq.recrdepend ( "
            "package		INTEGER, "
            "parent_recipe	INTEGER, "
            "parent_package	INTEGER )")

        return


    def assume_provided(self, item):
        return "%s:%s"%(item.type, item.name) in self._assume_provided


    def add_something(self, something, task_name):
        return (self.add_recipe(something, task_name) or
                self.add_provider(something, task_name))


    def add_provider(self, item, task_name):
        #print "add_provider(%s, %s)"%(item, task_name)
        package = self.get_provider(item, allow_no_provider=True)
        if not package:
            return False
        return self._add_package(package, task_name)


    def add_recipe(self, name, task_name):
        recipe = self.cookbook.get_recipe(name=name, strict=False)
        if not recipe:
            return False
        return self._add_recipe(recipe, task_name, primary=True)


    def add_package(self, item, task_name):
        package = self.cookbook.get_package(name=item.name, type=item.type)
        if not package:
            return False
        return self._add_package(package, task_name)


    def _add_package(self, package, task_name):
        if not package:
            return False
        return self._add_recipe(
            self.cookbook.get_recipe(package=package.id),
            task_name, primary=True)


    def _add_recipe(self, recipe, task_name, primary=False):
        if primary:
            primary_recipe = recipe
        else:
            primary_recipe = None
        #print "_add_recipe %s:%s"%(recipe, task_name)
        primary_task = self.cookbook.get_task(recipe=recipe, name=task_name)
        if not primary_task:
            raise NoSuchTask("%s:%s"%(recipe.name, task_name))

        alltasks = set()
        addedtasks = set([primary_task])

        while addedtasks:
            self.add_runq_tasks(addedtasks)

            alltasks.update(addedtasks)
            newtasks = set()
            for task in addedtasks:

                recipe = self.cookbook.get_recipe(task=task)

                if recipe == primary_recipe:
                    is_primary_recipe = True
                else:
                    is_primary_recipe = False

                # set rebuild flag (based on
                # --rebuild/--rebuildall/--reallyrebuildall)
                if ((self.rebuild >= 1 and is_primary_recipe) or
                    (self.rebuild == 2 and
                     recipe.get("REBUILDALL_SKIP") != "1") or
                    (self.rebuild == 3)):
                    self.set_task_build(task)

                # set relax flag (based on --sloppy/--relaxed)
                if ((self.relax == 2 and not is_primary_recipe) or
                    (self.relax == 1 and
                     (not is_primary_recipe and
                      recipe.get("RELAXED")))):
                    self.set_task_relax(task)

                try:
                    # task_dependencies should return tuple:
                    # task_depends: list of task_id's
                    # package_depends: list of (task_id, package_id)
                    # package_rdepends: list of (task_id, package_id)
                    (task_depends, package_depends, package_rdepends) = \
                        self.task_dependencies(task)
                    self.add_runq_task_depends(task, task_depends)
                    self.add_runq_package_depends(task, package_depends)
                    self.add_runq_package_rdepends(task, package_rdepends)
                    newtasks.update(task_depends)
                    newtasks.update([d[0] for d in package_depends])
                    newtasks.update([d[0] for d in package_rdepends])
                except RecursiveDepends, e:
                    recipe = self.cookbook.get_recipe(task=task)
                    raise RecursiveDepends(e.args[0], "%s (%s)"%(
                            recipe.name, task))

            addedtasks = newtasks.difference(alltasks)

        self.set_task_primary(primary_task)
        return True


    def task_dependencies(self, task):
        recipe = self.cookbook.get_recipe(task=task)

        # add recipe-internal task parents
        # (ie. addtask X before Y after Z)
        #parents = self.cookbook.get_task_parents(task) or []
        parents = task.get_parents()
        task_depends = set(parents)

        package_depends = set()
        package_rdepends = set()

        # helper to add multipe task_depends
        def add_task_depends(task_names, recipes):
            for task_name in task_names:
                for recipe in recipes:
                    task = self.cookbook.get_task(recipe, task_name)
                    if task:
                        task_depends.add(task)
                    else:
                        debug("not adding unsupported task %s:%s"%(
                                recipe, task_name))

        # helpers to add multipe package_depends / package_rdepends
        def add_package_depends(task_names, depends):
            return _add_package_depends(task_names, depends, package_depends)

        def add_package_rdepends(task_names, rdepends):
            return _add_package_depends(task_names, rdepends, package_rdepends)

        def _add_package_depends(task_names, depends, package_depends):
            for task_name in task_names:
                for (recipe, package) in depends:
                    if isinstance(recipe, oelite.recipe.OEliteRecipe):
                        recipe = recipe.id
                    task = self.cookbook.get_task(recipe=recipe, name=task_name)
                    if task:
                        package_depends.add((task, package))
                    else:
                        debug("not adding unsupported task %s:%s"%(
                                recipe, task_name))

        # add deptask dependencies
        # (ie. do_sometask[deptask] = "do_someothertask")
        deptasks = task.get_deptasks()
        if deptasks:
            # get list of (recipe, package) providing ${DEPENDS}
            depends = self.get_depends(recipe)
            # add each deptask for each (recipe, package)
            add_package_depends(deptasks, depends)

        # add rdeptask dependencies
        # (ie. do_sometask[rdeptask] = "do_someothertask")
        rdeptasks = task.get_rdeptasks()
        if rdeptasks:
            # get list of (recipe, package) providing ${RDEPENDS}
            rdepends = self.get_rdepends(recipe)
            # add each rdeptask for each (recipe, package)
            add_package_rdepends(rdeptasks, rdepends)

        # add recursive (build) depends tasks
        # (ie. do_sometask[recdeptask] = "do_someothertask")
        recdeptasks = task.get_recdeptasks()
        if recdeptasks:
            # get cumulative list of (recipe, package) providing
            # ${DEPENDS} and recursively the corresponding
            # ${PACKAGE_DEPENDS_*}
            depends = self.get_depends(recipe, recursive=True)
            # add each recdeptask for each (recipe, package)
            add_package_depends(recdeptasks, depends)

        # add recursive run-time depends tasks
        # (ie. do_sometask[recrdeptask] = "do_someothertask")
        recrdeptasks = task.get_recrdeptasks()
        if recrdeptasks:
            # get cumulative list of (recipe, package) providing
            # ${RDEPENDS} and recursively the corresponding
            # ${PACKAGE_RDEPENDS_*}
            rdepends = self.get_rdepends(recipe, recursive=True)
            # add each recdeptask of each (recipe, package)
            add_package_rdepends(recrdeptasks, rdepends)

        # add recursive all depends tasks
        # (ie. do_sometask[recadeptask] = "do_someothertask")
        recadeptasks = task.get_recadeptasks()
        if recadeptasks:
            # get all inclusive cumulative list of (recipe, package)
            # involved in a full build, including recursively all
            # ${DEPENDS}, ${RDEPENDS}, ${PACKAGE_DEPENDS_*} and
            # ${PACKAGE_RDEPENDS_*}
            depends = self.get_adepends(recipe, recursive=True)
            # get distinct recipe list
            depends = dict(depends).keys()
            # add each recdeptask for each (recipe, package)
            add_task_depends(recadeptasks, depends)

        # add inter-task dependencies
        # (ie. do_sometask[depends] = "itemname:do_someothertask")
        taskdepends = task.get_taskdepends()
        for taskdepend in taskdepends:
            task = self.cookbook.get_task(task=task)
            raise Exception("OE-lite does not currently support inter-task dependencies! %s:%s"%(recipe.name, task.name))
            if self.assume_provided(taskdepend[0]):
                #debug("ASSUME_PROVIDED %s"%(
                #        self.get_item(taskdepend[0])))
                continue
            (recipe, package) = self.get_recipe_provider(taskdepend[0])
            if not recipe:
                raise NoProvider(taskdepend[0])
            add_task_depends([taskdepend[1]], [recipe])

        # can self references occur?
        #if task in tasks:
        #    die("self reference for task %s %s"%(
        #            task, self.cookbook.get_task(task=task)))

        # we are _not_ checking for multiple providers of the same
        # thing, as it is considered (in theory) to be a valid
        # use-case

        # add task dependency information to runq db
        self.add_runq_task_depends(task, task_depends)
        self.add_runq_package_depends(task, package_depends)
        self.add_runq_package_rdepends(task, package_rdepends)

        return (task_depends, package_depends, package_rdepends)


    def get_depends(self, recipe, recursive=False):
        # this should not return items in ${ASSUME_PROVIDED}
        return self._get_depends(recipe, recursive, 0,
                                 self.cookbook.get_package_depends,
                                 recipe.get_depends,
                                 self.set_recdepends,
                                 self.get_recdepends)


    def get_rdepends(self, recipe, recursive=False):
        return self._get_depends(recipe, recursive, 1,
                                 self.cookbook.get_package_rdepends,
                                 recipe.get_rdepends,
                                 self.set_recrdepends,
                                 self.get_recrdepends)


    def _get_depends(self, recipe, recursive, typemap,
                     get_package_depends, recipe_get_depends,
                     set_recdepends, get_recdepends):

        def simple_resolve(item, recursion_path, type):
            item = oelite.item.OEliteItem(item, (typemap, type))
            if self.assume_provided(item):
                #debug("ASSUME_PROVIDED %s"%(item))
                return ([], [])
            (recipe, package) = self.get_recipe_provider(item)
            return ([recipe], [package])

        def recursive_resolve(item, recursion_path, type):
            #print "recursive_resolve %s %s"%(item, recursion_path)
            orig_item = item
            item = oelite.item.OEliteItem(item, (typemap, type))
            if self.assume_provided(item):
                #debug("ASSUME_PROVIDED %s"%(item))
                return set([])
            (recipe, package) = self.get_recipe_provider(item)

            # detect circular package dependencies
            if str(package) in recursion_path[0]:
                # actually, this might not be a bug/problem.......
                # Fx: package X rdepends on package Y, and package Y
                # rdepends on package X. As long as X and Y can be
                # built, anyone rdepend'ing on either X or Y will just
                # get both.  Bad circular dependencies must be
                # detected at runq task level.  If we cannot build a
                # recipe because of circular task dependencies, it is
                # clearly a bug.  Improve runq detection of this by
                # always simulating runq execution before starting,
                # and checking that all tasks can be completed, and if
                # some tasks are unbuildable, print out remaining
                # tasks and their dependencies.

                # on the other hand.... circular dependencies can be
                # arbitrarely complex, and it is pretty hard to handle
                # them generally, so better refuse to handle any of
                # them, to avoid having to add more and more complex
                # code to handle growingly sophisticated types of
                # circular dependencies.

                err("circular dependency while resolving %s"%(item))
                depends = []
                recursion_path[0].append(package)
                recursion_path[1].append(item)
                for i in xrange(len(recursion_path[0])):
                    depend_package = str(recursion_path[0][i])
                    depend_item = str(recursion_path[1][i])
                    if depend_item == depend_package:
                        depends.append(depend_package)
                    else:
                        depends.append("%s (%s)"%(depend_package, depend_item))
                raise RecursiveDepends(depends)

            # Recipe/task based circular dependencies are detected
            # later on when the entire runq has been constructed

            recursion_path[0].append(str(package))
            recursion_path[1].append(str(item))
            #print "recursion_path = %s"%(repr(recursion_path))

            # cache recdepends list of (recipe, package)
            recdepends = get_recdepends(package)
            if recdepends:
                recdepends.append((recipe, package))
                return recdepends

            recdepends = set()

            depends = get_package_depends(package)
            if depends:
                for depend in depends:
                    _recursion_path = copy.deepcopy(recursion_path)
                    _recdepends = recursive_resolve(depend, _recursion_path,
                                                    package.type)
                    recdepends.update(_recdepends)

            set_recdepends(package, recdepends)

            recdepends.update([(recipe, package)])
            return recdepends

        if recursive:
            resolve = recursive_resolve
        else:
            resolve = simple_resolve

        depends = recipe_get_depends()

        recdepends = set()

        for depend in depends:
            _recdepends = resolve(depend, ([], []), recipe.type)
            recdepends.update(_recdepends)

        return recdepends


    def get_adepends(self, recipe, recursive=True):
        return set()


    def get_recipe_provider(self, item):
        package = self.get_provider(item)
        if not package:
            raise NoProvider(item)
        recipe = self.cookbook.get_recipe(package=package)
        return (recipe, package)


    def _set_provider(self, item, package):
        self.dbc.execute(
            "INSERT INTO runq.provider (type, item, package) VALUES (?, ?, ?)",
            (item.type, item.name, package.id))
        return


    def _get_provider(self, item):
        package_id = flatten_single_value(self.dbc.execute(
                "SELECT package FROM runq.provider WHERE type=? AND item=?",
                (item.type, item.name)))
        if not package_id:
            return None
        return self.cookbook.get_package(id=package_id)

    def get_provider(self, item, allow_no_provider=False):
        """
        Return package provider of item.
        """
        assert isinstance(item, oelite.item.OEliteItem)
        provider = self._get_provider(item)
        if provider:
            #print "item=%s provider=%s"%(item, provider)
            #print "item.version=%s provider.version=%s"%(item.version, provider.version)
            assert item.version is None or item.version == provider.version
            return provider

        def choose_provider(providers):
            import bb.utils

            # filter out all but the highest priority providers
            highest_priority = providers[0].priority
            for i in range(1, len(providers)):
                if providers[i].priority != highest_priority:
                    del providers[i:]
                    break
            if len(providers) == 1:
                self._set_provider(item, providers[0])
                return providers[0]

            # filter out all but latest versions
            latest = {}
            for i in range(len(providers)):
                if not providers[i].name in latest:
                    latest[providers[i].name] = [ providers[i] ]
                    continue
                vercmp = bb.utils.vercmp_part(
                    latest[providers[i].name][0].version, providers[i].version)
                if vercmp < 0:
                    latest[providers[i].name] = [ providers[i] ]
                elif vercmp == 0:
                    latest[providers[i].name].append(providers[i])
            if len(latest) == 1:
                package = latest.values()[0][0]
                self._set_provider(item, package)
                return package
            if len(latest) > 1:
                multiple_providers = []
                for provider in latest.itervalues():
                    multiple_providers.append(str(provider[0]))
                raise MultipleProviders(
                    "multiple providers for %s: "%(item) + " ".join(multiple_providers))
            raise Exception("code path should never go here...")

        # first try with preferred provider and version
        # then try with prerred provider
        # and last any provider

        preferred_provider = (self.config.get(
                "PRFERRED_PROVIDER_%s_%s"%(item.type, item.name)) or
                              self.config.get(
                "PRFERRED_PROVIDER_%s"%(item.name)) or
                              None)

        if preferred_provider:
            preferred_version = (item.version
                                 or self.config.get(
                    "PREFERRED_VERSION_%s_%s"%(item.type, preferred_provider))
                                 or self.config.get(
                    "PREFERRED_VERSION_%s"%(preferred_provider))
                                 or None)
        else:
            preferred_version = item.version
        providers = self.cookbook.get_providers(
            item.type, item.name, preferred_provider, preferred_version)
        if len(providers) == 1:
            self._set_provider(item, providers[0])
            return providers[0]
        elif len(providers) > 1:
            return choose_provider(providers)
        if allow_no_provider:
            return None
        raise NoProvider(item)


    def update_task(self, task):

        get_recipe_datahash(task.recipe)
        if task is fetch:
            get_recipe_srchash(task.recipe)
        get_dependencies_hash(task)
        taskhash = hashit(recipehash, srchash, dephash)
        run=0
        if has_build(task):
            if datahash != build_datahash(task):
                info("recipe changes trigger run")
                run=1
            if srchash != build_srchash(task):
                info("src changes trigger run")
                run=1
            if dephash != build_dephash(task):
                info("dep changes trigger run")
                run=1
        else:
            info("no existing build")
            run=1
        if run:
            set_runable(task, datahash, srchash, dephash)
            # this marks task for run
            # and saves a combined taskhash for following
            # iterations (into runq_taskdepend.hash)
            # and all hashes for saving with build
            # result
            set_runq_taskdepend_checked(task)

        return


    def get_runabletask(self):
        newrunable = self.get_readytasks()
        if newrunable:
            if self.depth_first:
                self.runable += newrunable
            else:
                self.runable = newrunable + self.runable
            for task_id in newrunable:
                task = self.cookbook.get_task(id=task_id)
                self.set_task_pending(task)
        if not self.runable:
            return None
        task_id = self.runable.pop()
        if not task_id:
            return None
        return self.cookbook.get_task(id=task_id)


    def get_metahashable_task(self):
        if not self.metahashable:
            self.metahashable = list(self.get_metahashable_tasks())
        if not self.metahashable:
            return None
        task_id = self.metahashable.pop()
        if not task_id:
            return None
        return self.cookbook.get_task(id=task_id)


    def mark_done(self, task, delete=True):
        return self.set_task_done(task, delete)













    def get_recipes_with_tasks_to_build(self):
        # We need to use two different cursors
        dbc = self.dbc.connection.cursor()
        recipes = []
        for row in self.dbc.execute(
            "SELECT DISTINCT task.recipe "
            "FROM runq.task, task "
            "WHERE runq.task.build IS NOT NULL "
            "AND runq.task.task=task.id"):
            r = dbc.execute(
                "SELECT recipe.id, recipe.type, recipe.name, recipe.version, "
                "COUNT(runq.task.build) "
                "FROM runq.task, task, recipe "
                "WHERE recipe.id=? "
                "AND runq.task.task=task.id AND task.recipe=recipe.id ",
                (row[0],))
            recipes.append(r.fetchone())
        return recipes


    def print_runq_tasks(self):
        runq_tasks = self.dbc.execute(
            "SELECT prime,build,status,relax,metahash,tmphash,mtime,task "
            "FROM runq.task").fetchall()
        for row in runq_tasks:
            for col in row:
                print "%s "%(col),
            print "%s:%s"%(self.get_recipe(task=row[7]).get_name(),
                           self.cookbook.get_task(task=row[7]))
        return


    def get_tasks_to_build_description(self, hashinfo=False):
        tasks = []
        if hashinfo:
            hashinfo = ", runq.task.metahash, runq.task.tmphash"
        else:
            hashinfo = ""
        for row in self.dbc.execute(
            "SELECT recipe.type, recipe.name, recipe.version, task.name%s "
            "FROM runq.task, task, recipe "
            "WHERE runq.task.build IS NOT NULL "
            "AND runq.task.task=task.id "
            "AND task.recipe=recipe.id "
            "ORDER BY recipe.id DESC, task.name"%(hashinfo)):
            row = list(row)
            if row[0] == "machine":
                row[0] = ""
            else:
                row[0] += ":"
            row = tuple(row)
            if hashinfo:
                tasks.append("%s%s_%s:%s meta=%s tmp=%s"%row)
            else:
                tasks.append("%s%s_%s:%s"%row)
        return tasks


    def print_runq_depends(self):
        runq_depends = self.dbc.execute(
            "SELECT prime,task,parent_task,parent_package,parent_rpackage "
            "FROM runq.depend").fetchall()
        for row in runq_depends:
            prime = row[0] or 0
            print "%d %4d -> %4d %4s %4s  %s:%s -> %s:%s"%(
                prime, row[1], row[2], row[3], row[4],
                self.get_recipe(task=row[1]).get_name(),
                self.cookbook.get_task(task=row[1]),
                self.get_recipe(task=row[2]).get_name(),
                self.cookbook.get_task(task=row[2]))
        return

    def number_of_runq_tasks(self):
        return flatten_single_value(self.dbc.execute(
                "SELECT COUNT(*) FROM runq.task"))


    def number_of_tasks_to_build(self):
        return flatten_single_value(self.dbc.execute(
                "SELECT COUNT(*) FROM runq.task WHERE build IS NOT NULL"))


    def add_runq_task(self, task):
        assert isinstance(task, int)
        self.dbc.execute(
            "INSERT INTO runq.task (task) VALUES (?)", (task,))
        return


    def add_runq_tasks(self, tasks):
        def task_id_tuple(v):
            return (v.id,)
        tasks = map(task_id_tuple, tasks)
        self.dbc.executemany(
            "INSERT INTO runq.task (task) VALUES (?)", (tasks))
        return


    def add_runq_depend(self, task, depend,
                        depend_package=None, rdepend_package=None):
        assert isinstance(task, oelite.task.OEliteTask)
        assert isinstance(depend, oelite.task.OEliteTask)
        if depend_package:
            assert isinstance(depend_package, oelite.package.OElitePackage)
            self.dbc.execute(
                "INSERT INTO runq.depend (task, parent_task, depend_package) "
                "VALUES (?, ?, ?)", (task.id, depend.id, depend_package.id))
        elif rdepend_package:
            assert isinstance(rdepend_package, oelite.package.OElitePackage)
            self.dbc.execute(
                "INSERT INTO runq.depend (task, parent_task, rdepend_package) "
                "VALUES (?, ?, ?)", (task.id, depend.id, rdepend_package.id))
        else:
            self.dbc.execute(
                "INSERT INTO runq.depend (task, parent_task) "
                "VALUES (?, ?)", (task.id, depend.id))
        return


    def add_runq_task_depends(self, task, depends):
        def task_tuple(depend):
            return (task.id, depend.id)
        values = map(task_tuple, depends)
        self.dbc.executemany(
            "INSERT INTO runq.depend (task, parent_task) VALUES (?, ?)", values)
        return


    def add_runq_package_depends(self, task, depends):
        if not depends:
            return
        assert isinstance(task, oelite.task.OEliteTask)
        for depend in depends:
            self.add_runq_depend(task, depend[0], depend_package=depend[1])

    def add_runq_package_rdepends(self, task, depends):
        if not depends:
            return
        assert isinstance(task, oelite.task.OEliteTask)
        for depend in depends:
            self.add_runq_depend(task, depend[0], rdepend_package=depend[1])


    def set_package_filename(self, package, filename, prebake=False):
        assert isinstance(package, int)
        if prebake:
            self.dbc.execute(
                "UPDATE runq.depend SET filename=?, prebake=1 "
                "WHERE depend_package=? OR rdepend_package=?",
                (filename, package, package))
        else:
            self.dbc.execute(
                "UPDATE runq.depend SET filename=? "
                "WHERE depend_package=? OR rdepend_package=?",
                (filename, package, package))
        return


    def prune_prebaked_runq_depends(self):

        tasks = flatten_single_column_rows(self.dbc.execute(
            "SELECT"
            "  task "
            "FROM"
            "  runq.task "
            "WHERE"
            "  EXISTS " # something depends on it
            "    (SELECT *"
            "     FROM runq.depend"
            "     WHERE parent_task=runq.task.task"
            "     LIMIT 1)"
            "  AND NOT EXISTS " # and no task-based dependencies on it
            "    (SELECT * FROM runq.depend "
            "     WHERE runq.depend.parent_task=runq.task.task"
            "     AND (runq.depend.depend_package < 0 AND"
            "          runq.depend.rdepend_package < 0)"
            "     LIMIT 1)"
            "  AND NOT EXISTS " # and no non-prebaked dependencies on it
            "    (SELECT *"
            "     FROM runq.depend"
            "     WHERE runq.depend.parent_task=runq.task.task"
            "     AND (runq.depend.depend_package >= 0 OR"
            "          runq.depend.rdepend_package >= 0)"
            "     AND runq.depend.prebake IS NULL"
            "     LIMIT 1)"
            ))

        for task in tasks:
            debug("prebaked %s:%s"%(self.get_recipe(task=task).get_name(),
                                    self.cookbook.get_task(task=task)))
            self.dbc.execute(
                "UPDATE runq.depend SET parent_task=NULL WHERE parent_task=?",
                (task,))

        return


    def get_package_filename(self, package):
        #assert isinstance(package, int)
        assert isinstance(package, oelite.package.OElitePackage)
        return flatten_single_value(self.dbc.execute(
                "SELECT filename "
                "FROM runq.depend "
                "WHERE depend_package=? OR rdepend_package=? "
                "LIMIT 1", (package.id, package.id)))


    def set_recdepends(self, package, recdepends):
        return self._set_recdepends(
            package, recdepends, "runq.recdepend")

    def set_recrdepends(self, package, recrdepends):
        return self._set_recdepends(
            package, recrdepends, "runq.recrdepend")

    def _set_recdepends(self, package, recdepends, runq_recdepend):
        if not recdepends:
            return
        assert isinstance(package, oelite.package.OElitePackage)
        def task_tuple(depend):
            return (package.id, depend[0].id, depend[1].id)
        recdepends = map(task_tuple, recdepends)
        self.dbc.executemany(
            "INSERT INTO %s "%(runq_recdepend) +
            "(package, parent_recipe, parent_package) "
            "VALUES (?, ?, ?)", recdepends)
        return


    def get_recdepends(self, package):
        return self._get_recdepends(package, "runq.recdepend")

    def get_recrdepends(self, package):
        return self._get_recdepends(package, "runq.recrdepend")

    def _get_recdepends(self, package, runq_recdepend):
        assert isinstance(package, oelite.package.OElitePackage)
        recdepends = []
        for (recipe_id, package_id) in self.dbc.execute(
                "SELECT parent_recipe, parent_package "
                "FROM %s "%(runq_recdepend) +
                "WHERE package=?", (package.id,)).fetchall():
            recdepends.append((self.cookbook.get_recipe(id=recipe_id),
                               self.cookbook.get_package(id=package_id)))
        return recdepends


    def get_readytasks(self):
        return flatten_single_column_rows(self.dbc.execute(
                "SELECT"
                "  task "
                "FROM"
                "  runq.task "
                "WHERE"
                "  build=1 AND status IS NULL "
                "  AND ("
                "    NOT EXISTS"
                "    (SELECT * FROM runq.depend, runq.task AS parent_task"
                "     WHERE runq.depend.task=runq.task.task"
                "     AND runq.depend.parent_task IS NOT NULL"
                "     LIMIT 1)"
                "    OR NOT EXISTS"
                "    (SELECT * FROM runq.depend, runq.task AS parent_task"
                "     WHERE runq.depend.task=runq.task.task"
                "     AND runq.depend.parent_task=parent_task.task"
                "     AND parent_task.build IS NOT NULL"
                "     LIMIT 1))"))


    def print_metahashable_tasks(self):
        for r in self.dbc.execute("select task from runq.task where metahash is NULL"):
            print self.cookbook.get_task(id=r[0])
            for r in self.cookbook.db.execute("select parent_task, depend_package, rdepend_package from runq.depend where task=%s"%(r[0])):
                s = str(self.cookbook.get_task(id=r[0]))
                if r[1] != -1:
                    s += " depend_package=%s"%(self.cookbook.get_package(id=r[1]))
                if r[2] != -1:
                    s += " rdepend_package=%s"%(self.cookbook.get_package(id=r[2]))
                print " " +s

    def get_metahashable_tasks(self):
        return flatten_single_column_rows(self.dbc.execute(
                "SELECT task FROM runq.task "
                "WHERE metahash IS NULL AND NOT EXISTS "
                "(SELECT runq.depend.task"
                " FROM runq.depend, runq.task AS runq_task_depend"
                " WHERE runq.depend.task = runq.task.task"
                " AND runq.depend.parent_task = runq_task_depend.task"
                " AND runq_task_depend.metahash IS NULL"
                " LIMIT 1"
                ")"))


    def get_package_metahash(self, package):
        assert isinstance(package, int)
        return flatten_single_value(self.dbc.execute(
                "SELECT"
                "  runq.task.metahash "
                "FROM"
                "  runq.task, runq.depend "
                "WHERE"
                "  runq.depend.parent_task=runq.task.task"
                "  AND (runq.depend.depend_package=? OR"
                "       runq.depend.rdepend_package=?)"
                "  LIMIT 1", (package, package)))


    def get_package_metahash(self, package):
        return self._get_package_hash(package, "metahash")

    def get_package_buildhash(self, package):
        return self._get_package_hash(package, "buildhash")

    def _get_package_hash(self, package, hash):
        assert isinstance(package, int)
        return flatten_single_value(self.dbc.execute(
                "SELECT"
                "  runq.task.%s "
                "FROM"
                "  runq.task, runq.depend "
                "WHERE"
                "  runq.depend.parent_task=runq.task.task"
                "  AND (runq.depend.depend_package=? OR"
                "       runq.depend.rdepend_package=?) "
                "LIMIT 1"%(hash),
                (package, package)))


    def get_depend_packages(self, task=None):
        if task:
            assert isinstance(task, oelite.task.OEliteTask)
            return flatten_single_column_rows(self.dbc.execute(
                    "SELECT DISTINCT depend_package "
                    "FROM runq.depend "
                    "WHERE depend_package >= 0 AND task=?",
                (task.id,)))
        else:
            return flatten_single_column_rows(self.dbc.execute(
                    "SELECT DISTINCT depend_package "
                    "FROM runq.depend "
                    "WHERE depend_package >= 0"))


    def get_rdepend_packages(self, task=None):
        if task:
            assert isinstance(task, oelite.task.OEliteTask)
            return flatten_single_column_rows(self.dbc.execute(
                    "SELECT DISTINCT rdepend_package "
                    "FROM runq.depend "
                    "WHERE rdepend_package >= 0 AND task=?",
                (task.id,)))
        else:
            return flatten_single_column_rows(self.dbc.execute(
                    "SELECT DISTINCT rdepend_package "
                    "FROM runq.depend "
                    "WHERE rdepend_package >= 0"))


    def get_packages_to_build(self):
        depend_packages = flatten_single_column_rows(self.dbc.execute(
                "SELECT DISTINCT depend_package "
                "FROM runq.depend "
                "WHERE depend_package >= 0 AND prebake IS NULL"))
        rdepend_packages = flatten_single_column_rows(self.dbc.execute(
                "SELECT DISTINCT rdepend_package "
                "FROM runq.depend "
                "WHERE rdepend_package >= 0 AND prebake IS NULL"))
        return set(depend_packages).union(rdepend_packages)


    def set_buildhash_for_build_tasks(self):
        rowcount = self.dbc.execute(
            "UPDATE runq.task SET buildhash=metahash WHERE build=1"
            ).rowcount
        if rowcount == -1:
            die("unable to determine rowcount in "
                "set_buildhash_for_build_tasks")
        return rowcount


    def set_buildhash_for_nobuild_tasks(self):
        rowcount = self.dbc.execute(
            "UPDATE runq.task SET buildhash=tmphash WHERE build IS NULL"
            ).rowcount
        if rowcount == -1:
            die("unable to determine rowcount in "
                "set_buildhash_for_nobuild_tasks")
        return rowcount


    def mark_primary_runq_depends(self):
        rowcount = self.dbc.execute(
            "UPDATE runq.depend SET prime=1 WHERE EXISTS "
            "(SELECT * FROM runq.task"
            " WHERE runq.task.prime=1 AND runq.task.task=runq.depend.task"
            ")").rowcount
        if rowcount == -1:
            die("mark_primary_runq_depends did not work out")
        return rowcount


    def prune_runq_depends_nobuild(self):
        #c = self.dbc.cursor()
        rowcount = 0
        while self.dbc.rowcount:
            self.dbc.execute(
                "UPDATE runq.depend SET parent_task=NULL "
                "WHERE parent_task IS NOT NULL AND NOT EXISTS "
                "(SELECT * FROM runq.task"
                " WHERE runq.task.build=1"
                " AND runq.task.task=runq.depend.parent_task"
                " LIMIT 1"
                ")")
            if rowcount == -1:
                die("prune_runq_depends_nobuild did not work out")
            rowcount += self.dbc.rowcount
        if rowcount:
            debug("pruned %d dependencies that did not have to be rebuilt"%rowcount)
        return rowcount


    def prune_runq_depends_with_nobody_depending_on_it(self):
        #c = self.dbc.cursor()
        rowcount = 0
        while self.dbc.rowcount:
            self.dbc.execute(
                "DELETE FROM runq.depend "
                "WHERE prime IS NULL AND NOT EXISTS "
                "(SELECT * FROM runq.depend AS next_depend"
                " WHERE next_depend.parent_task=runq.depend.task"
                " LIMIT 1"
                ")")
            if rowcount == -1:
                die("prune_runq_depends_with_no_depending_tasks did not work out")
            rowcount += self.dbc.rowcount
        if rowcount:
            debug("pruned %d dependencies which where not needed anyway"%rowcount)
        return rowcount


    def prune_runq_tasks(self):
        rowcount = self.dbc.execute(
            "UPDATE"
            "  runq.task "
            "SET"
            "  build=NULL "
            "WHERE"
            "  prime IS NULL AND NOT EXISTS"
            "  (SELECT *"
            "   FROM runq.depend"
            "   WHERE runq.depend.parent_task=runq.task.task"
            "   LIMIT 1"
            ")").rowcount
        if rowcount == -1:
            die("prune_runq_tasks did not work out")
        if rowcount:
            debug("pruned %d tasks that does not need to be build"%rowcount)
        return rowcount


    def set_task_stamp(self, task, mtime, tmphash):
        assert isinstance(task, oelite.task.OEliteTask)
        self.dbc.execute(
            "UPDATE runq.task SET mtime=?, tmphash=? WHERE task=?",
            (mtime, tmphash, task.id))
        return


    def set_task_build(self, task):
        assert isinstance(task, oelite.task.OEliteTask)
        self.dbc.execute(
            "UPDATE runq.task SET build=1 WHERE task=?", (task.id,))
        return


    def set_task_relax(self, task):
        assert isinstance(task, oelite.task.OEliteTask)
        self.dbc.execute(
            "UPDATE runq.task SET relax=1 WHERE task=?", (task.id,))
        return


    def set_task_primary(self, task):
        assert isinstance(task, oelite.task.OEliteTask)
        self.dbc.execute(
            "UPDATE runq.task SET prime=1 WHERE task=?", (task.id,))
        return


    def is_runq_task_primary(self, task):
        assert isinstance(task, oelite.task.OEliteTask)
        primary = self.dbc.execute(
            "SELECT prime FROM runq.task WHERE task=?", (task.id,)).fetchone()
        return primary[0] == 1


    def is_runq_recipe_primary(self, recipe):
        primary = self.dbc.execute(
            "SELECT runq.task.prime "
            "FROM runq.task, task "
            "WHERE task.recipe=? AND runq.task.prime IS NOT NULL "
            "AND runq.task.task=task.id", (recipe,)).fetchone()
        return primary and primary[0] == 1


    def set_task_build_on_nostamp_tasks(self):
        rowcount = self.dbc.execute(
            "UPDATE runq.task SET build=1 "
            "WHERE build IS NULL AND EXISTS "
            "(SELECT * FROM task"
            " WHERE id=runq.task.task AND nostamp=1)").rowcount
        if rowcount == -1:
            die("set_task_build_on_nostamp_tasks did not work out")
        debug("set build flag on %d nostamp tasks"%(rowcount))
        return


    def set_task_build_on_retired_tasks(self):
        #c = self.dbc.cursor()
        rowcount = 0
        while self.dbc.rowcount:
            self.dbc.execute(
                "UPDATE runq.task SET build=1 "
                "WHERE build IS NULL AND EXISTS "
                "(SELECT * FROM runq.depend, runq.task AS parent_task"
                " WHERE runq.depend.task=runq.task.task"
                " AND runq.depend.parent_task=parent_task.task"
                " AND parent_task.mtime > runq.task.mtime)")
            if rowcount == -1:
                die("set_task_build_on_retired_tasks did not work out")
            rowcount += self.dbc.rowcount
        debug("set build flag on %d retired tasks"%(rowcount))
        return


    def set_task_build_on_hashdiff(self):
        #c = self.dbc.cursor()
        rowcount = 0
        while self.dbc.rowcount:
            self.dbc.execute(
                "UPDATE runq.task SET build=1 "
                "WHERE build IS NULL AND relax IS NULL AND tmphash != metahash")
            rowcount += self.dbc.rowcount
        debug("set build flag on %d tasks with tmphash != metahash"%(rowcount))
        return


    def propagate_runq_task_build(self):
        """always build all tasks depending on other tasks to build"""
        #c = self.dbc.cursor()
        rowcount = 0
        while self.dbc.rowcount:
            self.dbc.execute(
                "UPDATE"
                "  runq.task "
                "SET"
                "  build=1 "
                "WHERE"
                "  build IS NULL"
                "  AND EXISTS"
                "    (SELECT *"
                "     FROM runq.depend, runq.task AS parent_task"
                "     WHERE runq.depend.task=runq.task.task"
                "     AND runq.depend.parent_task=parent_task.task"
                "     AND parent_task.build=1"
                "     LIMIT 1)")
            rowcount += self.dbc.rowcount
        debug("set build flag on %d tasks due to propagation"%(rowcount))
        return


    def _set_task_status(self, task, status):
        assert isinstance(task, oelite.task.OEliteTask)
        self.dbc.execute(
            "UPDATE runq.task SET status=? WHERE task=?", (status, task.id))
        return


    def set_task_pending(self, task):
        return self._set_task_status(task, 1)


    def set_task_running(self, task):
        return self._set_task_status(task, 2)


    def set_task_done(self, task, delete):
        assert isinstance(task, oelite.task.OEliteTask)
        self._set_task_status(task, 3)
        #if delete:
        #    self.dbc.execute(
        #        "DELETE FROM runq.depend WHERE parent_task=?", (task.id,))
        self.dbc.execute(
            "UPDATE runq.depend SET parent_task=NULL "
            "WHERE parent_task=?", (task.id,))
        return


    def set_task_failed(self, task):
        return self._set_task_status(task, -1)


    def prune_done_tasks(self):
        self.dbc.execute(
            "DELETE FROM runq.depend WHERE EXISTS "
            "( SELECT * FROM runq.task "
            "WHERE runq.task.task = runq.depend.parent_task AND status=3 )")
        return


    def set_task_metahash(self, task, metahash):
        assert isinstance(task, oelite.task.OEliteTask)
        self.dbc.execute(
            "UPDATE runq.task SET metahash=? WHERE task=?",
            (metahash, task.id))
        return


    def get_task_metahash(self, task):
        assert isinstance(task, oelite.task.OEliteTask)
        return flatten_single_value(self.dbc.execute(
            "SELECT metahash FROM runq.task WHERE task=?", (task.id,)))


    def get_buildhash(self, task):
        assert isinstance(task, oelite.task.OEliteTask)
        return flatten_single_value(self.dbc.execute(
                "SELECT buildhash FROM runq.task WHERE task=?", (task.id,)))


    def get_task_buildhash(self, task):
        assert isinstance(task, oelite.task.OEliteTask)
        return flatten_single_value(self.dbc.execute(
            "SELECT buildhash FROM runq.task WHERE task=?", (task.id,)))
