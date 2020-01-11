#!/usr/bin/python3
import numpy as np
from numpy import sin, cos
from geometry_msgs.msg import Twist,Vector3,PoseStamped
from std_msgs.msg import Bool
from hamilton_ac.msg import Reference
import rospy

class AdaptiveController():
    #implements adaptive controller for ouijabot in 2D manipulation
    #a = [m,J,m*rpx,m*rpy,u1,u1*rix,u1*riy,u1*||ri||,rix,riy]
    def __init__(self):
        self.getParams()
        self.active = False
        self.controllerReset()
        self.q = self.q_prev = np.zeros(3)
        self.q_des, self.dq_des = np.zeros(3), np.zeros(3)
        self.ddq_des = np.zeros(3)
        self.state_time = -1

        self.wrap_tol = 0.1

        self.cmd_pub = rospy.Publisher('cmd_global',Twist,queue_size=1)
        self.state_sub = rospy.Subscriber('state',PoseStamped,
            self.stateCallback)

        self.ref_sub = rospy.Subscriber('/ac/ref',Reference,self.refCallback)
        self.active_sub = rospy.Subscriber('/ac/active', Bool,
            self.activeCallback)
        self.state_pub = rospy.Publisher('state_est',Reference,queue_size=1)
        self.cmd_timer = rospy.Timer(rospy.Duration(0.1),
            self.controllerCallback)

    def controllerReset(self):
        self.dq = np.zeros(3)
        self.tau, self.F = np.zeros(3), np.zeros(3)
        self.a_hat = np.zeros(10)

    def getParams(self):
        self.L = rospy.get_param('/ac/L')*np.eye(3)
        self.Kd_lin = rospy.get_param('/ac/Kd_lin')
        self.Kd_ang = rospy.get_param('/ac/Kd_ang')
        self.Kd = np.diag([self.Kd_lin,self.Kd_lin,self.Kd_ang])
        self.Gamma = rospy.get_param('/ac/Gamma')*np.eye(10)
        self.pos_elems = [0,1,4,7] #flags which elements to project to >0
        self.deadband = rospy.get_param('/ac/deadband')
        self.q_filt = rospy.get_param('/ac/q_filt')
        self.dq_filt = rospy.get_param('/ac/dq_filt')
        self.offset_angle = rospy.get_param('offset_angle','0.') #angle offset
            #from payload frame, default to zero

    def activeCallback(self,msg):
        if not self.active and msg.data:
            self.getParams()
        elif self.active and not msg.data:
            self.controllerReset()

        self.active = msg.data

    def controllerCallback(self,event):
        """defines a timer callback to implement controller"""
        #define dynamics terms

        if self.active:
            dt = event.current_real.to_sec() - event.last_real.to_sec()
            q_err = self.q - self.q_des
            dq_err = self.dq - self.dq_des
            s = dq_err + self.L@q_err
            dq_r = self.dq_des - self.L@q_err
            ddq_r = self.ddq_des - self.L@dq_err

            #control law
            self.F = self.Y() @ self.a_hat - self.Kd @ s #world frame
            self.tau = self.Mhat_inv() @ self.F #world frame

            #adaptation law:
            if np.linalg.norm(s) > self.deadband:
                param_derivative = self.Gamma @ (self.Y()+self.Z()).T @ s
                self.a_hat = self.a_hat - dt*(param_derivative)
            # TODO: (Preston): implement Heun's method for integration;
                #do projection step here & finish w/next value of s above.

                #projection step:
                self.a_hat[self.pos_elems] = np.maximum(
                    self.a_hat[self.pos_elems],0.)

        #publish command in world frame; use force_global to rotate
        lin_cmd = Vector3(x=self.tau[0],y=self.tau[1],z=0.)
        ang_cmd = Vector3(x=0.,y=0.,z=self.tau[2])
        cmd_msg = Twist(linear=lin_cmd,angular=ang_cmd)
        self.cmd_pub.publish(cmd_msg)

        msg = Reference(Vector3(*self.q),Vector3(*self.dq),Vector3())
        self.state_pub.publish(msg)

    def stateCallback(self,data):
        '''handles measurement callback from Optitrack'''
        if self.state_time == -1:
            self.state_time = data.header.stamp.to_sec()
        else:
            dt = data.header.stamp.to_sec() - self.state_time
            th = quaternion_to_angle(data.pose.orientation)
            q_new = np.array([data.pose.position.x,data.pose.position.y,th])
            q_smoothed = (1-self.q_filt)*q_new + self.q_filt*self.q

            q_smoothed, self.q, self.q_prev = self.wrap_angles(q_smoothed,
                self.q, self.q_prev)

            dq_new = (3*q_smoothed - 4*self.q + self.q_prev)/(2*dt)

            self.q_prev = self.q
            self.q = q_smoothed
            self.dq = (1-self.dq_filt)*dq_new + self.dq_filt*self.dq
            self.state_time= data.header.stamp.to_sec()

    def refCallback(self,data):
        self.q_des = np.array([data.q_des.x,data.q_des.y,data.q_des.z])
        self.dq_des = np.array([data.dq_des.x,data.dq_des.y,data.dq_des.z])
        self.ddq_des = np.array([data.ddq_des.x,data.ddq_des.y,data.ddq_des.z])

    def Mhat_inv(self):
        """defines correction term for moment arms in control law"""
        rhx, rhy = self.a_hat[-2:]
        _, _, th = self.q

        rhx_n = rhx*cos(th)+rhy*sin(th)
        rhy_n = -rhx*sin(th)+rhy*cos(th)

        return np.array([[1,0,0],[0,1,0],[rhy_n,-rhx_n,1]])

    def wrap_angles(self,q_new,q_curr,q_prev):
        if abs(q_new - q_curr) >= 2*np.pi - self.wrap_tol:
            q_curr = q_curr + 2*np.pi if q_new > q_curr else q_curr - 2*np.pi

        if abs(q_curr - q_prev) >= 2*np.pi - self.wrap_tol:
            q_prev = q_prev + 2*np.pi if q_curr > q_prev else q_prev - 2*np.pi

        return q_new, q_curr, q_prev

    def Y(self):
        """Y*a = H*ddqr + (C+D)dqr"""
        x, y, th = self.q
        dx, dy, dth = self.dq
        dxr, dyr, dthr = self.dq_des
        ddxr, ddyr, ddthr = self.ddq_des
        block1h = np.array([[ddxr,0,-sin(th)*ddthr, cos(th)*ddthr],
            [ddyr,0,-cos(th)*ddthr,-sin(th)*ddthr],
            [0,ddthr,-sin(th)*ddxr-cos(th)*ddyr,cos(th)*ddxr-sin(th)*ddyr]])
        block1c = np.array([[0,0,dth*dthr,0],[0,0,0,dth*dthr],[0,0,0,0]])
        block2 = np.array([[dxr,sin(th)*dthr,-cos(th)*dthr,0],
            [dyr,cos(th)*dthr,sin(th)*dthr,0],
            [0,sin(th)*dxr+cos(th)*dyr,-cos(th)*dxr+sin(th)*dyr,dthr]])
        Y = np.concatenate((block1h+block1c,block2,np.zeros((3,2))),axis=1)
        return Y

    def Z(self):
        """F + Z(q,F)@(ahat-a) = G*inv(Ghat)*F"""
        _ ,_ ,th = self.q
        Fx, Fy, _ = self.F
        block = np.array([[0,0],[0,0],
            [-(sin(th)*Fx+cos(th)*Fy),cos(th)*Fx-sin(th)*Fy]])
        return np.concatenate((np.zeros((3,8)),block),axis=1)

def quaternion_to_angle(q):
    """transforms quaternion to body angle in plane"""
    cos_th = 2*(q.x**2 + q.w**2)-1
    sin_th = -2*(q.x*q.y - q.z*q.w)
    return np.arctan2(sin_th,cos_th)

def main():
    rospy.init_node('hamilton_ac')
    try:
        AdaptiveController()
        rospy.logwarn('starting ac')
        rospy.spin()
    except rospy.ROSException as e:
        rospy.logwarn('closing ac')
        raise e

if __name__ == '__main__':
    main()
