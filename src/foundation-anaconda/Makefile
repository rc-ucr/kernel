#
# $Id: Makefile,v 1.3 2012/11/27 00:49:07 phil Exp $
#
# @Copyright@
# 
# 				Rocks(r)
# 		         www.rocksclusters.org
# 		         version 6.2 (SideWinder)
# 		         version 7.0 (Manzanita)
# 
# Copyright (c) 2000 - 2017 The Regents of the University of California.
# All rights reserved.	
# 
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are
# met:
# 
# 1. Redistributions of source code must retain the above copyright
# notice, this list of conditions and the following disclaimer.
# 
# 2. Redistributions in binary form must reproduce the above copyright
# notice unmodified and in its entirety, this list of conditions and the
# following disclaimer in the documentation and/or other materials provided 
# with the distribution.
# 
# 3. All advertising and press materials, printed or electronic, mentioning
# features or use of this software must display the following acknowledgement: 
# 
# 	"This product includes software developed by the Rocks(r)
# 	Cluster Group at the San Diego Supercomputer Center at the
# 	University of California, San Diego and its contributors."
# 
# 4. Except as permitted for the purposes of acknowledgment in paragraph 3,
# neither the name or logo of this software nor the names of its
# authors may be used to endorse or promote products derived from this
# software without specific prior written permission.  The name of the
# software includes the following terms, and any derivatives thereof:
# "Rocks", "Rocks Clusters", and "Avalanche Installer".  For licensing of 
# the associated name, interested parties should contact Technology 
# Transfer & Intellectual Property Services, University of California, 
# San Diego, 9500 Gilman Drive, Mail Code 0910, La Jolla, CA 92093-0910, 
# Ph: (858) 534-5815, FAX: (858) 534-7345, E-MAIL:invent@ucsd.edu
# 
# THIS SOFTWARE IS PROVIDED BY THE REGENTS AND CONTRIBUTORS ``AS IS''
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO,
# THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR
# PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE REGENTS OR CONTRIBUTORS
# BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR
# BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY,
# WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE
# OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN
# IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
# 
# @Copyright@
#
#
#
#

REDHAT.ROOT     = $(CURDIR)/../../
HTTPGET		= $(ROCKSROOT)/bin/httpget.sh
-include $(ROCKSROOT)/etc/Rules.mk
include Rules.mk


rpm::	$(HOME)/.rpmmacros prespec $(NAME).spec
	rpmbuild -bb $(REDHAT.ROOT)/SPECS/$(NAME).spec


prespec::
# ROCKS8 - create foundation-anaconda from CentOS 7.9.2009 anaconda
# http://vault.centos.org/7.9.2009/os/Source/SPackages/anaconda-21.48.22.159-1.el7.centos.src.rpm
ifeq ($(shell ! test -f $(SRCPKGNAME) && echo -n yes),yes)
	@echo "ROCKS8: Sideloading $(SRCPKGNAME)."
	$(HTTPGET) -B http://vault.centos.org -F 7.9.2009/os/Source/SPackages -n $(SRCPKGNAME)
endif
	(								\
		mkdir content && cd content; 				\
		rpm2cpio ../$(SRCPKGNAME) | cpio -idmv;			\
		tar xf $(TARFILENAME);					\
		mv anaconda-$(VERSION) foundation-anaconda-$(VERSION);	\
		tar cjf $(NEWTARNAME) foundation-anaconda-$(VERSION);	\
		cd ..;							\
		mv -v content/*.patch ../../SOURCES;			\
		mv -v content/$(NEWTARNAME) ../../SOURCES;		\
		rm -rf content; 					\
	)
#       spec file is created separately from anaconda.
clean::
	rm -rf $(NAME).buildroot
	rm -rf ../../BUILD/$(NAME)*
	rm -f $(SRCPKGNAME)
