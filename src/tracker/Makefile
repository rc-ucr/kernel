#
# $Id: Makefile,v 1.8 2010/03/19 16:45:08 bruno Exp $
#
# @COPYRIGHT@
# @COPYRIGHT@
#
# $Log: Makefile,v $
# Revision 1.8  2010/03/19 16:45:08  bruno
# converted the client to a 'fast cgi' script.
#
# Revision 1.7  2010/03/15 23:05:56  bruno
# tweaks
#
# Revision 1.6  2010/03/10 19:45:43  bruno
# more debug
#
# Revision 1.5  2010/03/09 17:40:54  bruno
# tune the debugging
#
# Revision 1.4  2010/03/03 21:58:30  bruno
# tweak
#
# Revision 1.3  2010/03/03 19:21:03  bruno
# add code to 'unregister' a file and clean up the hash table when there are
# no more peers for a hash.
#
# Revision 1.2  2010/02/25 05:45:48  bruno
# makin' progress
#
# Revision 1.1  2010/02/24 20:27:32  bruno
# moved new tracker from base roll to kernel roll
#
# Revision 1.4  2009/09/25 21:02:04  bruno
# got prediction code in
#
# Revision 1.3  2009/09/17 20:12:49  bruno
# lots of good stuff:
#  - expandable, circular hash table
#  - clients randomly shuffle their peer list
#  - skip to next peer if download of peer fails
#
# Revision 1.2  2009/09/15 21:52:13  bruno
# closer
#
# Revision 1.1  2009/09/08 22:17:49  bruno
# start of the new tracker
#
#

REDHAT.ROOT     = $(CURDIR)/../../

-include $(ROCKSROOT)/etc/Rules.mk
include Rules.mk

INCLUDE	= -I/usr/include/apr-1 -I/opt/rocks/fcgi/include
#LIBS	= -L/opt/rocks/lib -lcurl
#LIBS	= -lcurl -lssl -lidn -lgssapi_krb5 -lkrb5 -lk5crypto -lkrb5support -lpthread -lcrypto -lz -ldl -lcom_err -lkeyutils -lresolv -lselinux -lsepol
LIBS	= -lcurl
#EXTRA	= -Wall -DDEBUG -pg -D_LARGEFILE_SOURCE -D_FILE_OFFSET_BITS=64
EXTRA	= -Wall -D_LARGEFILE_SOURCE -D_FILE_OFFSET_BITS=64
EXECS	= tracker-client unregister-file server peer-done stop-server

build:	$(EXECS)

tracker-client:	tracker-client.c client.c lib.c
	cc $(INCLUDE) $(EXTRA) -DFASTCGI -o tracker-client tracker-client.c \
		client.c lib.c $(LIBS) /opt/rocks/fcgi/lib/libfcgi.a

unregister-file:	unregister-file.c client.c lib.c
	cc $(INCLUDE) $(EXTRA) -o unregister-file unregister-file.c \
		client.c lib.c $(LIBS)

server:		server.o lib.o
	cc $(INCLUDE) $(EXTRA) -pg -o server server.o lib.o $(LIBS)

lib.o:	lib.c
	cc $(INCLUDE) $(EXTRA) -c lib.c

server.o:	server.c
	cc $(INCLUDE) $(EXTRA) -c server.c

hashit:		hashit.c
	cc $(INCLUDE) $(EXTRA) -o hashit hashit.c lib.c $(LIBS)

peer-done:	peer-done.c
	cc $(INCLUDE) $(EXTRA) -o peer-done peer-done.c client.c lib.c \
		$(LIBS)

stop-server:	stop-server.c
	cc $(INCLUDE) $(EXTRA) -o stop-server stop-server.c client.c lib.c \
		$(LIBS)

install::
	mkdir -p $(ROOT)/$(PKGROOT)/bin
	cp $(EXECS) $(ROOT)/$(PKGROOT)/bin

clean::
	rm -f $(NAME).spec.in
	rm -f $(EXECS)
	rm -f *.o
