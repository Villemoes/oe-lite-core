import sys, os
from oebakery import die, err, warn, info, debug
from oelite import InvalidRecipe

class OEliteRecipe:

    def __init__(self, filename, extend, data, db):
        self.db = db
        self.data = data

        name = data.getVar("PN", 1)
        if not name:
            raise InvalidRecipe("no PN in %s:%s"%(
                    filename, extend))

        version = data.getVar("PV", 1) or "0"
        if "PR" in data:
            version += "-" + data.getVar("PR", 1)
        
        preference = data.getVar("DEFAULT_PREFERENCE", 1) or "0"
        try:
            preference = int(preference)
        except ValueError, e:
            raise InvalidRecipe("invalid DEFAULT_PREFERENCE=%s in %s:%s"%(
                    preference, filename, extend))

        self.db.add_recipe(filename, extend, name, version, preference)
        recipe_id = self.db.get_recipe_id(filename, extend)

        depends = data.getVar("DEPENDS", 1) or ""
        for depend in depends.split():
            self.db.add_item(depend)
            self.db.add_recipe_depend(recipe_id, depend)

        rdepends = data.getVar("RDEPENDS", 1) or ""
        for rdepend in rdepends.split():
            self.db.add_ritem(rdepend)
            self.db.add_recipe_rdepend(recipe_id, rdepend)

        task_deps = data.getVar("_task_deps", 0)

        for task in task_deps["tasks"]:
            self.db.add_task(recipe_id, task)

        for task in task_deps["tasks"]:
            task_id = self.db.get_task_id(recipe_id, task)

            try:
                for parent in task_deps["parents"][task]:
                    self.db.add_task_parent(task_id, parent, recipe=recipe_id)
            except KeyError, e:
                pass

            try:
                for deptask in task_deps["deptask"][task].split():
                    self.db.add_task_deptask(task_id, deptask)
            except KeyError, e:
                pass

            try:
                for rdeptask in task_deps["rdeptask"][task].split():
                    self.db.add_task_rdeptask(task_id, rdeptask)
            except KeyError, e:
                pass

            try:
                for recdeptask in task_deps["recdeptask"][task].split():
                    self.db.add_task_recdeptask(task_id, recdeptask)
            except KeyError, e:
                pass

            try:
                for recrdeptask in task_deps["recrdeptask"][task].split():
                    self.db.add_task_recrdeptask(task_id, recrdeptask)
            except KeyError, e:
                pass

            try:
                for depend in task_deps["depends"][task].split():
                    depend_split = depend.split(":")
                    if len(depend_split) != 2:
                        err("invalid task 'depends' value "
                            "(valid syntax is item:task): %s"%(depend))
                    self.db.add_task_depend(task_id,
                                            depend_item=depend_split[0],
                                            depend_task=depend_split[1])
            except KeyError, e:
                pass

            try:
                if task_deps["nostamp"][task]:
                    self.db.set_task_nostamp(task_id)
            except KeyError, e:
                pass


        packages = data.getVar("PACKAGES", 1)
        if not packages:
            warn("no PACKAGES in recipe %s"%name)
            return

        for package in packages.split():

            arch = (self.data.getVar("PACKAGE_ARCH_" + package, True) or
                    self.data.getVar("RECIPE_ARCH", True))
            self.db.add_package(recipe_id, package, arch)
            package_id = self.db.get_package_id(recipe_id, package)

            provides = data.getVar("PROVIDES_" + package, 1) or ""
            for item in provides.split():
                self.db.add_provider(package_id, item)

            rprovides = data.getVar("RPROVIDES_" + package, 1) or ""
            for ritem in rprovides.split():
                self.db.add_rprovider(package_id, ritem)

            depends = data.getVar("DEPENDS_" + package, 1) or ""
            for item in depends.split():
                self.db.add_package_depend(package_id, item)

            rdepends = data.getVar("RDEPENDS_" + package, 1) or ""
            for ritem in rdepends.split():
                self.db.add_package_rdepend(package_id, ritem)

        #self.db.commit()

        self.id = recipe_id

        return


    def prepare(self):

        def set_pkgproviders(self_db_get_recipe_depends,
                             self_db_get_runq_recdepends,
                             self_db_get_runq_provider,
                             PKGPROVIDER_, RECDEPENDS):
            recdepends = []

            def set_pkgprovider(item,
                                self_db_get_runq_provider,
                                PKGPROVIDER_):
                debug("set_pkgprovider(%s)"%(item))
                package_id = self_db_get_runq_provider(item)
                (package_name, package_arch) = self.db.get_package(package_id)
                (recipe_name, recipe_version) = self.db.get_recipe(
                        {"package": package_id})
                pkgprovider = "%s/%s-%s"%(
                        package_arch, package_name, recipe_version)
                recdepends.append(package_name)
                debug("recdepends=%s"%(str(recdepends)))
                self.data.setVar(PKGPROVIDER_ + package_name, pkgprovider)
                return package_id

            depends = self_db_get_recipe_depends(self.id) or []
            for item in depends:
                provider = set_pkgprovider(
                    item, self_db_get_runq_provider, PKGPROVIDER_)
                for item in (self_db_get_runq_recdepends(provider)[1] or []):
                    set_pkgprovider(
                        item, self_db_get_runq_provider, PKGPROVIDER_)

            debug("%s=%s"%(RECDEPENDS, " ".join(recdepends)))
            self.data.setVar(RECDEPENDS, " ".join(recdepends))

        # FIXME: only do this for recdeptasks
        set_pkgproviders(self.db.get_recipe_depends,
                         self.db.get_runq_recdepends,
                         self.db.get_runq_provider,
                         "PKGPROVIDER_", "RECDEPENDS")

        # FIXME: only do this for recrdeptasks
        set_pkgproviders(self.db.get_recipe_rdepends,
                         self.db.get_runq_recrdepends,
                         self.db.get_runq_rprovider,
                         "PKGRPROVIDER_", "RECRDEPENDS")

